"""Collect per-channel video history and daily stats.

Usage:
    python -m src.collect_channels --mode backfill
    python -m src.collect_channels --mode incremental

See docs/collect-channels-spec.md for the full design.
"""
import argparse
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

import requests

from src import db, parsers, quota
from src.storage_client import StorageError, SupabaseStorageClient, save_thumbnail
from src.youtube_client import QuotaExceededError, YouTubeAPIError, YouTubeClient, chunked, save_raw_response

log = logging.getLogger("collect_channels")
ROOT = Path(__file__).resolve().parent.parent


def load_settings() -> dict:
    with open(ROOT / "config" / "settings.toml", "rb") as f:
        return tomllib.load(f)


def build_storage_client() -> SupabaseStorageClient:
    """Thumbnails are optional: if SUPABASE_URL/SUPABASE_SERVICE_KEY aren't set, callers
    just skip thumbnail mirroring rather than failing the whole collection run."""
    project_url = os.environ.get("SUPABASE_URL")
    service_key = os.environ.get("SUPABASE_SERVICE_KEY")
    if not project_url or not service_key:
        log.warning("SUPABASE_URL/SUPABASE_SERVICE_KEY not set — skipping thumbnail mirroring")
        return None
    storage = SupabaseStorageClient(project_url, service_key)
    storage.ensure_bucket()
    return storage


def _maybe_save_thumbnail(storage, conn, video_id: str, thumbnail_url: str, is_new: bool) -> None:
    if not is_new or storage is None or not thumbnail_url:
        return
    try:
        path = save_thumbnail(storage, video_id, thumbnail_url)
        db.set_thumbnail_path(conn, video_id, path)
    except (requests.RequestException, StorageError) as exc:
        log.warning("thumbnail save failed for %s: %s", video_id, exc)


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


def _fetch_videos_batch(client: YouTubeClient, conn, video_ids: list, run_id: str,
                         collected_at: str, unit_cost: int, raw_dir: Path, label: str,
                         storage=None, save_thumbnails: bool = False) -> None:
    """save_thumbnails must stay False for backfill: almost every backfilled video is
    "new to our DB" on first encounter, and downloading thumbnails for hundreds of
    thousands of historical videos is exactly the cost the user opted out of — only
    the incremental (genuinely newly-published) path passes save_thumbnails=True."""
    for i, batch in enumerate(chunked(video_ids)):
        if not batch:
            continue
        resp = client.videos_list(part="snippet,contentDetails,statistics", ids=batch, unit_cost=unit_cost)
        save_raw_response(resp, raw_dir / f"{run_id}_{label}_videos_{i}.json.gz")
        for item in resp.get("items", []):
            video_row = _extract_video_row(item, collected_at)
            is_new = db.upsert_video(conn, video_row)
            if save_thumbnails:
                _maybe_save_thumbnail(storage, conn, video_row["video_id"], video_row["thumbnail_url"], is_new)
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
        conn.commit()


def _fetch_channel_snapshot(client: YouTubeClient, conn, channel_ids: list, run_id: str,
                             collected_at: str, unit_cost: int, raw_dir: Path) -> dict:
    """channels.list in batches of 50. Returns {channel_id: uploads_playlist_id}."""
    uploads_by_channel = {}
    for i, batch in enumerate(chunked(channel_ids)):
        if not batch:
            continue
        resp = client.channels_list(ids=batch, unit_cost=unit_cost)
        save_raw_response(resp, raw_dir / f"{run_id}_channels_{i}.json.gz")
        for item in resp.get("items", []):
            channel_id = item["id"]
            stats = item.get("statistics", {})
            content = item.get("contentDetails", {})
            uploads_by_channel[channel_id] = content.get("relatedPlaylists", {}).get("uploads")
            db.insert_channel_stats(conn, {
                "channel_id": channel_id,
                "collected_at": collected_at,
                "run_id": run_id,
                "subscriber_count": stats.get("subscriberCount"),
                "view_count": stats.get("viewCount"),
                "video_count": stats.get("videoCount"),
            })
        conn.commit()
    return uploads_by_channel


def _backfill_channel(client: YouTubeClient, conn, channel: dict, uploads_playlist_id: str,
                       settings: dict, run_id: str, collected_at: str, raw_dir: Path) -> None:
    channel_id = channel["channel_id"]
    max_videos = settings["channels"]["backfill"]["max_videos_per_channel"]
    batch_size = settings["channels"]["backfill"]["batch_size"]
    max_pages = -(-max_videos // batch_size)  # ceil

    video_ids = []
    page_token = None
    for page in range(max_pages):
        resp = client.playlist_items_list(
            uploads_playlist_id, max_results=batch_size, page_token=page_token,
            unit_cost=settings["quota"]["unit_cost_playlist_items_list"],
        )
        save_raw_response(resp, raw_dir / f"{run_id}_{channel_id}_{page}.json.gz")
        for pi in resp.get("items", []):
            video_ids.append(pi["contentDetails"]["videoId"])
        page_token = resp.get("nextPageToken")
        if not page_token or len(video_ids) >= max_videos:
            break
    video_ids = video_ids[:max_videos]

    _fetch_videos_batch(client, conn, video_ids, run_id, collected_at,
                         settings["quota"]["unit_cost_videos_list"], raw_dir, channel_id)

    oldest_published_at = None
    if video_ids:
        placeholders = ",".join("%s" for _ in video_ids)
        row = conn.execute(
            f"SELECT MIN(published_at) AS m FROM videos WHERE video_id IN ({placeholders})",
            video_ids,
        ).fetchone()
        oldest_published_at = row["m"] if row else None

    db.upsert_channel_collection_state(
        conn, channel_id,
        backfill_status="done",
        backfill_completed_at=collected_at,
        oldest_video_published_at=oldest_published_at,
    )
    conn.commit()


def run_backfill(client: YouTubeClient, conn, settings: dict, run_id: str, collected_at: str, raw_dir: Path) -> int:
    queue = db.get_backfill_queue(conn)
    if not queue:
        log.info("backfill queue is empty")
        return 0

    # cross-run accounting: several backfill batches run per day (settings.channels.backfill.runs_per_day),
    # so each one must see what earlier runs already spent today rather than assuming a full budget.
    today = collected_at[:10]
    used_today = db.get_quota_used_today(conn, today)
    reserved = settings["quota"]["trending_reserved"] + settings["quota"]["channel_incremental_reserved"]
    remaining_budget = max(settings["quota"]["daily_cap"] - used_today - reserved, 0)

    per_channel_pages = -(-settings["channels"]["backfill"]["max_videos_per_channel"]
                           // settings["channels"]["backfill"]["batch_size"])
    quota_slots = quota.backfill_slots_today(
        remaining_budget, per_channel_pages,
        settings["quota"]["unit_cost_playlist_items_list"],
        settings["quota"]["unit_cost_videos_list"],
    )

    # spread the daily target evenly across the day's runs instead of one run draining the budget
    daily_target = settings["channels"]["backfill"]["daily_channel_target"]
    runs_per_day = settings["channels"]["backfill"]["runs_per_day"]
    per_run_cap = -(-daily_target // runs_per_day)
    slots = min(quota_slots, per_run_cap)

    todays_batch = queue[:slots]
    if not todays_batch:
        log.info(
            "no backfill slots this run (quota_slots=%d, per_run_cap=%d, queue=%d channels waiting)",
            quota_slots, per_run_cap, len(queue),
        )
        return 0
    log.info("backfilling %d/%d queued channels this run (used_today=%d before this run)",
              len(todays_batch), len(queue), used_today)

    channel_ids = [c["channel_id"] for c in todays_batch]
    uploads_by_channel = _fetch_channel_snapshot(
        client, conn, channel_ids, run_id, collected_at,
        settings["quota"]["unit_cost_channels_list"], raw_dir,
    )

    processed = 0
    for channel in todays_batch:
        channel_id = channel["channel_id"]
        uploads_playlist_id = uploads_by_channel.get(channel_id)
        if not uploads_playlist_id:
            log.warning("channel %s has no uploads playlist, marking failed", channel_id)
            db.upsert_channel_collection_state(conn, channel_id, backfill_status="failed")
            conn.commit()
            continue
        try:
            _backfill_channel(client, conn, channel, uploads_playlist_id, settings, run_id, collected_at, raw_dir)
            processed += 1
        except YouTubeAPIError as exc:
            log.warning("backfill failed for channel %s: %s", channel_id, exc)
            db.upsert_channel_collection_state(conn, channel_id, backfill_status="failed")
            conn.commit()
    return processed


def _detect_new_videos(client: YouTubeClient, conn, channel: dict, uploads_playlist_id: str,
                        settings: dict, run_id: str, collected_at: str, raw_dir: Path) -> list:
    channel_id = channel["channel_id"]
    max_pages = settings["channels"]["incremental"]["new_video_max_pages"]
    safe_buffer_days = settings["channels"]["incremental"]["safe_buffer_days"]

    new_video_ids = []
    page_token = None
    for page in range(max_pages):
        resp = client.playlist_items_list(
            uploads_playlist_id, page_token=page_token,
            unit_cost=settings["quota"]["unit_cost_playlist_items_list"],
        )
        save_raw_response(resp, raw_dir / f"{run_id}_{channel_id}_new_{page}.json.gz")
        items = resp.get("items", [])
        page_ids = [pi["contentDetails"]["videoId"] for pi in items]
        if not page_ids:
            break

        placeholders = ",".join("%s" for _ in page_ids)
        known = {r["video_id"] for r in conn.execute(
            f"SELECT video_id FROM videos WHERE video_id IN ({placeholders})", page_ids
        ).fetchall()}
        new_video_ids.extend([vid for vid in page_ids if vid not in known])

        oldest_on_page = min(
            (pi["contentDetails"].get("videoPublishedAt") for pi in items if pi["contentDetails"].get("videoPublishedAt")),
            default=None,
        )
        all_known = all(vid in known for vid in page_ids)
        stale = False
        if oldest_on_page:
            cutoff = parsers.parse_utc(collected_at) - __import__("datetime").timedelta(days=safe_buffer_days)
            stale = parsers.parse_utc(oldest_on_page) < cutoff
        if all_known and stale:
            break

        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    else:
        log.warning("channel %s hit max page cap (%d) during new-video detection", channel_id, max_pages)

    return new_video_ids


def run_incremental(client: YouTubeClient, conn, settings: dict, run_id: str, collected_at: str,
                     raw_dir: Path, storage=None) -> int:
    channels = db.get_included_channels(conn)
    if not channels:
        log.info("no included channels for incremental collection")
        return 0

    channel_ids = [c["channel_id"] for c in channels]
    uploads_by_channel = _fetch_channel_snapshot(
        client, conn, channel_ids, run_id, collected_at,
        settings["quota"]["unit_cost_channels_list"], raw_dir,
    )

    all_video_ids = set()
    for channel in channels:
        channel_id = channel["channel_id"]
        uploads_playlist_id = uploads_by_channel.get(channel_id)
        if not uploads_playlist_id:
            log.warning("channel %s has no uploads playlist, skipping", channel_id)
            continue
        try:
            new_ids = _detect_new_videos(client, conn, channel, uploads_playlist_id, settings, run_id, collected_at, raw_dir)
        except YouTubeAPIError as exc:
            log.warning("new-video detection failed for channel %s: %s", channel_id, exc)
            new_ids = []
        all_video_ids.update(new_ids)
        db.upsert_channel_collection_state(conn, channel_id, last_incremental_at=collected_at)
    conn.commit()

    growth_window_days = settings["channels"]["incremental"]["growth_window_days"]
    growth_ids = db.get_growth_window_video_ids(conn, channel_ids, growth_window_days)
    all_video_ids.update(growth_ids)

    _fetch_videos_batch(client, conn, list(all_video_ids), run_id, collected_at,
                         settings["quota"]["unit_cost_videos_list"], raw_dir, "incremental",
                         storage=storage, save_thumbnails=True)

    return len(all_video_ids)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["backfill", "incremental"], required=True)
    args = parser.parse_args()

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
    job_name = f"channel_{args.mode}"
    db.start_run(conn, run_id, job_name, collected_at)

    client = YouTubeClient(api_key)
    raw_dir = ROOT / settings["storage"]["raw_dir"] / "channel" / collected_at[:4] / collected_at[5:7] / collected_at[8:10]

    status = "success"
    error_message = None
    items_seen = 0
    try:
        if args.mode == "backfill":
            items_seen = run_backfill(client, conn, settings, run_id, collected_at, raw_dir)
        else:
            storage = build_storage_client()
            items_seen = run_incremental(client, conn, settings, run_id, collected_at, raw_dir, storage)
    except QuotaExceededError as exc:
        status = "failed"
        error_message = str(exc)
        log.error("quota exceeded, aborting run: %s", exc)

    db.finish_run(conn, run_id, parsers.now_utc_iso(), status, client.quota_used, items_seen, error_message)
    conn.close()

    log.info("run %s (%s) finished: status=%s items_seen=%d quota_used=%d",
              run_id, args.mode, status, items_seen, client.quota_used)
    return 0 if status != "failed" else 1


if __name__ == "__main__":
    sys.exit(main())
