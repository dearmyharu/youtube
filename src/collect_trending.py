"""Collect YouTube 'mostPopular' trending videos (overall + per category).

Usage:
    python -m src.collect_trending

See docs/trending-collector-spec.md for the full design.
"""
import json
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
from src.youtube_client import QuotaExceededError, YouTubeAPIError, YouTubeClient, save_raw_response

log = logging.getLogger("collect_trending")
ROOT = Path(__file__).resolve().parent.parent


def load_settings() -> dict:
    with open(ROOT / "config" / "settings.toml", "rb") as f:
        return tomllib.load(f)


def _extract_video_row(item: dict, first_seen_at: str) -> dict:
    snippet = item.get("snippet", {})
    content = item.get("contentDetails", {})
    return {
        "video_id": item["id"],
        "channel_id": snippet.get("channelId"),
        "title": snippet.get("title"),
        "published_at": snippet.get("publishedAt"),
        "duration_sec": parsers.parse_iso8601_duration(content.get("duration", "")),
        "category_id": snippet.get("categoryId"),
        "tags": json.dumps(snippet.get("tags", []), ensure_ascii=False),
        "thumbnail_url": (snippet.get("thumbnails", {}).get("high") or snippet.get("thumbnails", {}).get("default") or {}).get("url"),
        "first_seen_at": first_seen_at,
        "last_meta_at": first_seen_at,
    }


def _collect_chart(client: YouTubeClient, conn, settings: dict, category_id, run_id: str,
                    collected_at: str, raw_dir: Path) -> int:
    """Collect one chart (category_id=None means the overall 'all' chart). Returns items seen."""
    region_code = settings["youtube"]["region_code"]
    max_pages = settings["trending"]["max_pages_per_category"]
    label = str(category_id) if category_id else "all"
    items_seen = 0
    page_token = None
    for page in range(max_pages):
        try:
            resp = client.videos_list(
                part="snippet,contentDetails,statistics",
                chart="mostPopular",
                region_code=region_code,
                video_category_id=str(category_id) if category_id else None,
                page_token=page_token,
                unit_cost=settings["quota"]["unit_cost_videos_list"],
            )
        except YouTubeAPIError as exc:
            log.warning("category %s failed, skipping: %s", label, exc)
            return items_seen

        save_raw_response(resp, raw_dir / f"{run_id}_{label}_{page}.json.gz")

        items = resp.get("items", [])
        for rank, item in enumerate(items, start=1):
            video_row = _extract_video_row(item, collected_at)
            db.upsert_video(conn, video_row)
            db.record_meta_history_if_changed(
                conn, video_row["video_id"], collected_at, video_row["title"], video_row["thumbnail_url"]
            )
            stats = item.get("statistics", {})
            db.insert_video_stats(conn, {
                "video_id": video_row["video_id"],
                "collected_at": collected_at,
                "run_id": run_id,
                "view_count": stats.get("viewCount"),
                "like_count": stats.get("likeCount"),
                "comment_count": stats.get("commentCount"),
            })
            db.insert_trending_rank(conn, {
                "collected_at": collected_at,
                "run_id": run_id,
                "region": region_code,
                "category_id": label,
                "rank": rank,
                "video_id": video_row["video_id"],
            })
            db.upsert_channel_pool(conn, {
                "channel_id": video_row["channel_id"],
                "channel_title": item.get("snippet", {}).get("channelTitle"),
                "first_seen_at": collected_at,
                "source": "trending",
                "screened_at": None,
                "passed_filter": None,
                "group_type": None,
                # auto-approved into the panel so the daily backfill/incremental jobs pick it
                # up without manual review; ON CONFLICT DO NOTHING means a later manual
                # decision (e.g. exclude, or promoting to tier='core') is never overwritten
                "decision": "include",
                "decision_reason": None,
            })
        items_seen += len(items)
        conn.commit()

        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return items_seen


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
    db.start_run(conn, run_id, "trending", collected_at)

    client = YouTubeClient(api_key)
    raw_dir = ROOT / settings["storage"]["raw_dir"] / "trending" / collected_at[:4] / collected_at[5:7] / collected_at[8:10]

    categories = settings["trending"]["categories"]
    total_items = 0
    failed_categories = []
    status = "success"
    error_message = None

    try:
        total_items += _collect_chart(client, conn, settings, None, run_id, collected_at, raw_dir)
        for category_id in categories:
            try:
                total_items += _collect_chart(client, conn, settings, category_id, run_id, collected_at, raw_dir)
            except YouTubeAPIError as exc:
                failed_categories.append(category_id)
                log.warning("category %s failed: %s", category_id, exc)
    except QuotaExceededError as exc:
        status = "failed"
        error_message = str(exc)
        log.error("quota exceeded, aborting run: %s", exc)
    else:
        if failed_categories:
            status = "partial"
            error_message = f"failed categories: {failed_categories}"

    db.finish_run(conn, run_id, parsers.now_utc_iso(), status, client.quota_used, total_items, error_message)
    conn.close()

    log.info("run %s finished: status=%s items_seen=%d quota_used=%d", run_id, status, total_items, client.quota_used)

    if total_items == 0:
        log.error("collected 0 items — treating as failure")
        return 1
    return 0 if status != "failed" else 1


if __name__ == "__main__":
    sys.exit(main())
