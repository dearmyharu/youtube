"""Read-only SQL for the dashboard. Every function here is a plain SELECT against
either an observation table or one of the precomputed aggregate tables
(monthly_keyword_trend / monthly_format_trend) — nothing here re-tokenizes titles
or scans `videos` live. See src/build_dashboard_aggregates.py for what fills those
aggregate tables.
"""
import json

import pandas as pd


def get_job_freshness(conn) -> pd.DataFrame:
    rows = conn.execute("""
        SELECT DISTINCT ON (job_name) job_name, status, started_at, finished_at, error_message
        FROM runs
        ORDER BY job_name, started_at DESC
    """).fetchall()
    return pd.DataFrame([dict(r) for r in rows])


def get_channel_counts(conn) -> dict:
    decision_rows = conn.execute(
        "SELECT decision, COUNT(*) AS n FROM channel_pool GROUP BY decision"
    ).fetchall()
    by_decision = {r["decision"]: r["n"] for r in decision_rows}

    backfill_rows = conn.execute(
        "SELECT backfill_status, COUNT(*) AS n FROM channel_collection_state GROUP BY backfill_status"
    ).fetchall()
    by_backfill = {r["backfill_status"]: r["n"] for r in backfill_rows}

    return {
        "included": by_decision.get("include", 0),
        "excluded": by_decision.get("exclude", 0),
        "backfill_done": by_backfill.get("done", 0),
        "backfill_failed": by_backfill.get("failed", 0),
    }


def get_monthly_format_trend(conn) -> pd.DataFrame:
    rows = conn.execute("SELECT * FROM monthly_format_trend ORDER BY year_month").fetchall()
    df = pd.DataFrame([dict(r) for r in rows])
    if df.empty:
        return df
    df["upload_hour_histogram"] = df["upload_hour_histogram"].apply(json.loads)
    df["upload_weekday_histogram"] = df["upload_weekday_histogram"].apply(json.loads)
    return df


def get_monthly_keyword_trend_dict(conn, months: int = 6) -> dict:
    """Reconstruct {year_month: {keyword: count}} for the last N months present in
    the aggregate table, in the shape src.analysis.keywords functions expect."""
    month_rows = conn.execute(
        "SELECT DISTINCT year_month FROM monthly_keyword_trend ORDER BY year_month DESC LIMIT %s",
        (months,),
    ).fetchall()
    target_months = [r["year_month"] for r in month_rows]
    if not target_months:
        return {}
    placeholders = ",".join("%s" for _ in target_months)
    rows = conn.execute(
        f"SELECT year_month, keyword, video_count FROM monthly_keyword_trend WHERE year_month IN ({placeholders})",
        target_months,
    ).fetchall()
    out = {}
    for r in rows:
        out.setdefault(r["year_month"], {})[r["keyword"]] = r["video_count"]
    return out


def get_recent_thumbnails(conn, limit: int = 60) -> pd.DataFrame:
    """Videos with a mirrored thumbnail (new-discoveries only, see CLAUDE.md), newest first."""
    rows = conn.execute("""
        SELECT v.video_id, v.title, v.published_at, v.thumbnail_path, cp.channel_title,
               lvs.view_count
        FROM videos v
        JOIN channel_pool cp ON cp.channel_id = v.channel_id
        LEFT JOIN LATERAL (
            SELECT view_count FROM video_stats vs
            WHERE vs.video_id = v.video_id ORDER BY collected_at DESC LIMIT 1
        ) lvs ON true
        WHERE v.thumbnail_path IS NOT NULL
        ORDER BY v.first_seen_at DESC
        LIMIT %s
    """, (limit,)).fetchall()
    return pd.DataFrame([dict(r) for r in rows])


def get_channel_leaderboard(conn, limit: int = 30) -> pd.DataFrame:
    """Median view count per channel (from each video's latest observation), subscriber
    count (from the channel's latest snapshot), and upload cadence over the last 90 days."""
    rows = conn.execute("""
        WITH latest_video_stats AS (
            SELECT DISTINCT ON (video_id) video_id, view_count
            FROM video_stats ORDER BY video_id, collected_at DESC
        ),
        latest_channel_stats AS (
            SELECT DISTINCT ON (channel_id) channel_id, subscriber_count
            FROM channel_stats ORDER BY channel_id, collected_at DESC
        ),
        recent_uploads AS (
            SELECT channel_id, COUNT(*) AS uploads_last_90d
            FROM videos
            WHERE published_at::timestamptz >= NOW() - INTERVAL '90 days'
            GROUP BY channel_id
        )
        SELECT
            cp.channel_title,
            cp.tier,
            lcs.subscriber_count,
            percentile_cont(0.5) WITHIN GROUP (ORDER BY lvs.view_count) AS median_views,
            COUNT(v.video_id) AS video_count,
            COALESCE(ru.uploads_last_90d, 0) AS uploads_last_90d
        FROM videos v
        JOIN channel_pool cp ON cp.channel_id = v.channel_id
        JOIN latest_video_stats lvs ON lvs.video_id = v.video_id
        LEFT JOIN latest_channel_stats lcs ON lcs.channel_id = cp.channel_id
        LEFT JOIN recent_uploads ru ON ru.channel_id = cp.channel_id
        WHERE cp.decision = 'include'
        GROUP BY cp.channel_title, cp.tier, lcs.subscriber_count, ru.uploads_last_90d
        HAVING COUNT(v.video_id) >= 5
        ORDER BY median_views DESC
        LIMIT %s
    """, (limit,)).fetchall()
    return pd.DataFrame([dict(r) for r in rows])
