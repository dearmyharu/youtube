"""Precompute the dashboard's aggregate tables (monthly_keyword_trend, monthly_format_trend).

The dashboard (dashboard/app.py) only ever SELECTs from these — it never re-runs
soynlp tokenization or scans `videos` on page load. Re-run this whenever you want
the dashboard to reflect newly-collected data (daily is reasonable).

Usage:
    python -m src.build_dashboard_aggregates [--min-keyword-frequency N]
"""
import argparse
import json
import logging
import os
from pathlib import Path

from dotenv import load_dotenv

from src import db, parsers
from src.analyze_trends import fetch_videos, run_format_trends, run_keyword_trends

log = logging.getLogger("build_dashboard_aggregates")
ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--min-keyword-frequency", type=int, default=5,
                         help="drop keywords below this monthly count to keep the table small")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    load_dotenv(ROOT / ".env")

    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        log.error("DATABASE_URL is not set")
        return 1

    conn = db.get_connection(database_url)
    db.init_db(conn)

    videos = fetch_videos(conn)
    log.info("loaded %d videos", len(videos))
    if not videos:
        log.error("no videos to aggregate")
        return 1

    computed_at = parsers.now_utc_iso()

    log.info("computing format trends ...")
    format_summary = run_format_trends(videos)
    for year_month, summary in format_summary.items():
        db.upsert_monthly_format_trend(conn, {
            "year_month": year_month,
            "video_count": summary["video_count"],
            "shorts_count": summary["shorts_count"],
            "shorts_ratio": summary["shorts_ratio"],
            "avg_duration_longform_sec": summary["avg_duration_longform_sec"],
            "avg_duration_shorts_sec": summary["avg_duration_shorts_sec"],
            "series_notation_ratio": summary["series_notation_ratio"],
            "upload_hour_histogram": json.dumps(summary["upload_hour_kst_histogram"]),
            "upload_weekday_histogram": json.dumps(summary["upload_weekday_kst_histogram"]),
            "computed_at": computed_at,
        })
    conn.commit()
    log.info("wrote monthly_format_trend for %d months", len(format_summary))

    log.info("computing keyword trends (soynlp) ...")
    monthly_freq = run_keyword_trends(videos)
    total_kept = 0
    for year_month, counts in monthly_freq.items():
        kept = {kw: c for kw, c in counts.items() if c >= args.min_keyword_frequency}
        db.replace_monthly_keyword_trend(conn, year_month, kept, computed_at)
        total_kept += len(kept)
    conn.commit()
    log.info("wrote monthly_keyword_trend: %d months, %d keyword rows total (>= %d occurrences)",
              len(monthly_freq), total_kept, args.min_keyword_frequency)

    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
