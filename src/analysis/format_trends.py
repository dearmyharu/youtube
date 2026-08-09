"""Format trend analysis (형식 트렌드): duration, upload timing, shorts ratio,
series-notation usage — all derivable straight from videos.published_at/duration_sec,
so unlike view-velocity this is fully retroactive (no need to wait for new data).

Upload time is bucketed in KST per the project's UTC-storage/KST-analysis principle
(docs/trending-collector-spec.md #0.3) — never bucket by the raw UTC hour.
"""
from collections import Counter, defaultdict

from src.analysis.keywords import is_series_notation
from src.parsers import is_short_candidate, utc_to_kst


def year_month_kst(published_at_utc: str) -> str:
    kst = utc_to_kst(published_at_utc)
    return f"{kst.year:04d}-{kst.month:02d}"


def monthly_format_summary(rows: list, short_max_sec: int = 180) -> dict:
    """rows: [{"published_at": iso_utc_str, "duration_sec": int, "title": str}, ...]

    Returns {year_month: {video_count, shorts_count, shorts_ratio,
    avg_duration_longform_sec, avg_duration_shorts_sec, series_notation_ratio,
    upload_hour_kst_histogram: {0..23: n}, upload_weekday_kst_histogram: {0..6: n}}}
    (weekday 0=Monday, per datetime.weekday()).
    """
    buckets = defaultdict(lambda: {
        "video_count": 0,
        "shorts_count": 0,
        "longform_durations": [],
        "shorts_durations": [],
        "series_count": 0,
        "hour_hist": Counter(),
        "weekday_hist": Counter(),
    })

    for row in rows:
        published_at = row.get("published_at")
        duration_sec = row.get("duration_sec")
        if not published_at or duration_sec is None:
            continue
        kst = utc_to_kst(published_at)
        year_month = year_month_kst(published_at)
        b = buckets[year_month]
        b["video_count"] += 1

        is_short = is_short_candidate(duration_sec, max_duration_sec=short_max_sec)
        if is_short:
            b["shorts_count"] += 1
            b["shorts_durations"].append(duration_sec)
        else:
            b["longform_durations"].append(duration_sec)

        if is_series_notation(row.get("title", "")):
            b["series_count"] += 1

        b["hour_hist"][kst.hour] += 1
        b["weekday_hist"][kst.weekday()] += 1

    summary = {}
    for year_month, b in buckets.items():
        n = b["video_count"]
        summary[year_month] = {
            "video_count": n,
            "shorts_count": b["shorts_count"],
            "shorts_ratio": b["shorts_count"] / n if n else 0.0,
            "avg_duration_longform_sec": (
                sum(b["longform_durations"]) / len(b["longform_durations"])
                if b["longform_durations"] else None
            ),
            "avg_duration_shorts_sec": (
                sum(b["shorts_durations"]) / len(b["shorts_durations"])
                if b["shorts_durations"] else None
            ),
            "series_notation_ratio": b["series_count"] / n if n else 0.0,
            "upload_hour_kst_histogram": dict(b["hour_hist"]),
            "upload_weekday_kst_histogram": dict(b["weekday_hist"]),
        }
    return summary
