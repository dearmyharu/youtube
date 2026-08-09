"""One-off channel discovery bootstrap: search.list(order=viewCount) across creator-genre
keywords, so the channel pool skews toward high-view creator content instead of whatever
happens to surface in the daily trending chart.

Not scheduled — run manually when you want to grow/refresh the candidate list:
    python -m src.discover_channels

See docs/collect-channels-spec.md and config/settings.toml [discovery] for the keyword list
and quota assumptions (search.list is expensive: ~100 units/call vs 1 unit for list endpoints).
"""
import logging
import os
import sys
import uuid
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # Python < 3.11
    import tomli as tomllib

from dotenv import load_dotenv

from src import db, parsers
from src.youtube_client import QuotaExceededError, YouTubeAPIError, YouTubeClient, chunked, save_raw_response

log = logging.getLogger("discover_channels")
ROOT = Path(__file__).resolve().parent.parent


def load_settings() -> dict:
    with open(ROOT / "config" / "settings.toml", "rb") as f:
        return tomllib.load(f)


def _search_keyword(client: YouTubeClient, settings: dict, keyword: str, run_id: str,
                     collected_at: str, raw_dir: Path) -> dict:
    """Returns {video_id: (channel_id, channel_title)} for one keyword's top-viewed videos."""
    cfg = settings["discovery"]
    try:
        resp = client.search_list(
            q=keyword,
            region_code=settings["youtube"]["region_code"],
            order="viewCount",
            max_results=cfg["max_results_per_keyword"],
            unit_cost=cfg["unit_cost_search_list"],
        )
    except YouTubeAPIError as exc:
        log.warning("search for %r failed, skipping: %s", keyword, exc)
        return {}

    save_raw_response(resp, raw_dir / f"{run_id}_search_{keyword}.json.gz")

    found = {}
    for item in resp.get("items", []):
        video_id = item.get("id", {}).get("videoId")
        snippet = item.get("snippet", {})
        channel_id = snippet.get("channelId")
        if not video_id or not channel_id:
            continue
        found[video_id] = (channel_id, snippet.get("channelTitle"))
    return found


def _filter_by_category(client: YouTubeClient, settings: dict, video_ids: list, run_id: str,
                         raw_dir: Path) -> set:
    """Batch-fetch categoryId for each video, return the subset of video_ids NOT in
    discovery.exclude_category_ids."""
    cfg = settings["discovery"]
    excluded = set(cfg["exclude_category_ids"])
    allowed_video_ids = set()
    for i, batch in enumerate(chunked(video_ids)):
        if not batch:
            continue
        resp = client.videos_list(part="snippet", ids=batch, unit_cost=settings["quota"]["unit_cost_videos_list"])
        save_raw_response(resp, raw_dir / f"{run_id}_categories_{i}.json.gz")
        for item in resp.get("items", []):
            category_id = item.get("snippet", {}).get("categoryId")
            if category_id not in excluded:
                allowed_video_ids.add(item["id"])
    return allowed_video_ids


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    load_dotenv(ROOT / ".env")

    api_key = os.environ.get("YOUTUBE_API_KEY")
    if not api_key:
        log.error("YOUTUBE_API_KEY is not set")
        return 1
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        log.error("DATABASE_URL is not set")
        return 1

    settings = load_settings()
    conn = db.get_connection(database_url)
    db.init_db(conn)

    run_id = uuid.uuid4().hex
    collected_at = parsers.now_utc_iso()
    db.start_run(conn, run_id, "channel_discovery", collected_at)

    client = YouTubeClient(api_key)
    raw_dir = ROOT / settings["storage"]["raw_dir"] / "discovery" / collected_at[:4] / collected_at[5:7] / collected_at[8:10]

    keywords = settings["discovery"]["keywords"]
    status = "success"
    error_message = None
    video_to_channel = {}

    try:
        for keyword in keywords:
            found = _search_keyword(client, settings, keyword, run_id, collected_at, raw_dir)
            video_to_channel.update(found)
            log.info("keyword %r: %d videos found so far total=%d", keyword, len(found), len(video_to_channel))
    except QuotaExceededError as exc:
        status = "partial"
        error_message = str(exc)
        log.error("quota exceeded mid-search, proceeding with what was found so far: %s", exc)

    channels_inserted = 0
    if video_to_channel:
        try:
            allowed_video_ids = _filter_by_category(client, settings, list(video_to_channel), run_id, raw_dir)
        except QuotaExceededError as exc:
            status = "partial"
            error_message = str(exc)
            log.error("quota exceeded during category filtering: %s", exc)
            allowed_video_ids = set()

        seen_channels = {}
        for video_id in allowed_video_ids:
            channel_id, channel_title = video_to_channel[video_id]
            seen_channels[channel_id] = channel_title

        for channel_id, channel_title in seen_channels.items():
            db.upsert_channel_pool(conn, {
                "channel_id": channel_id,
                "channel_title": channel_title,
                "first_seen_at": collected_at,
                "source": "search_discovery",
                "screened_at": None,
                "passed_filter": None,
                "group_type": None,
                "decision": "include",
                "decision_reason": None,
            })
        conn.commit()
        channels_inserted = len(seen_channels)

    db.finish_run(conn, run_id, parsers.now_utc_iso(), status, client.quota_used, channels_inserted, error_message)
    conn.close()

    log.info(
        "run %s finished: status=%s unique_videos=%d unique_channels=%d quota_used=%d",
        run_id, status, len(video_to_channel), channels_inserted, client.quota_used,
    )
    return 0 if status != "failed" else 1


if __name__ == "__main__":
    sys.exit(main())
