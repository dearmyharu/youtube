"""Run A (title keyword trend) + B (format trend) analysis over the collected videos.

Both are fully retroactive — no need to wait for new time-series data, unlike view
velocity / growth rate. See docs/collect-channels-spec.md and the project's trend
analysis design notes for the A/B/C split.

Usage:
    python -m src.analyze_trends [--months N]
"""
import argparse
import logging
import os
from collections import defaultdict
from pathlib import Path

from dotenv import load_dotenv

from src import db
from src.analysis import keywords as kw
from src.analysis.format_trends import monthly_format_summary, year_month_kst

log = logging.getLogger("analyze_trends")
ROOT = Path(__file__).resolve().parent.parent


def fetch_videos(conn) -> list:
    """Only videos from currently-included channels, and never News & Politics (25) —
    a channel can be `include`d for its usual content but still occasionally post a
    news-category video, and old rows from since-excluded channels linger in `videos`
    (exclusion doesn't delete history, it just stops future tracking)."""
    rows = conn.execute("""
        SELECT v.video_id, v.channel_id, v.title, v.published_at, v.duration_sec
        FROM videos v
        JOIN channel_pool cp ON cp.channel_id = v.channel_id
        WHERE v.published_at IS NOT NULL AND v.duration_sec IS NOT NULL AND v.title IS NOT NULL
          AND cp.decision = 'include'
          AND v.category_id != '25'
    """).fetchall()
    return [dict(r) for r in rows]


def run_format_trends(videos: list) -> dict:
    return monthly_format_summary(videos)


def run_keyword_trends(videos: list) -> dict:
    titles = [v["title"] for v in videos if v["title"]]
    log.info("training soynlp word extractor on %d titles ...", len(titles))
    word_scores = kw.train_word_extractor(titles)
    tokenizer = kw.build_tokenizer(word_scores)
    log.info("extracted %d candidate words", len(word_scores))

    channel_video_tokens = defaultdict(list)
    video_tokens = {}
    for v in videos:
        toks = kw.tokenize_title(tokenizer, v["title"])
        video_tokens[v["video_id"]] = toks
        channel_video_tokens[v["channel_id"]].append(toks)

    fixed_phrases_by_channel = kw.find_channel_fixed_phrases(channel_video_tokens)
    n_channels_with_fixed = sum(1 for phrases in fixed_phrases_by_channel.values() if phrases)
    log.info("detected channel-fixed phrases for %d/%d channels", n_channels_with_fixed, len(channel_video_tokens))

    records = []
    for v in videos:
        toks = video_tokens[v["video_id"]]
        toks = kw.strip_channel_phrases(toks, fixed_phrases_by_channel.get(v["channel_id"], set()))
        records.append((year_month_kst(v["published_at"]), toks))

    return kw.monthly_keyword_frequency(records)


def print_report(format_summary: dict, monthly_freq: dict, months_to_show: int) -> None:
    months = sorted(set(format_summary) | set(monthly_freq))[-months_to_show:]

    print("\n=== B. 형식 트렌드 (월별) ===")
    for ym in months:
        fs = format_summary.get(ym)
        if not fs:
            continue
        print(f"\n[{ym}] 영상 {fs['video_count']}개, 쇼츠 비율 {fs['shorts_ratio']:.0%}, "
              f"시리즈 표기 비율 {fs['series_notation_ratio']:.0%}")
        if fs["avg_duration_longform_sec"] is not None:
            print(f"  롱폼 평균 길이: {fs['avg_duration_longform_sec']/60:.1f}분")
        if fs["avg_duration_shorts_sec"] is not None:
            print(f"  쇼츠 평균 길이: {fs['avg_duration_shorts_sec']:.0f}초")
        top_hours = sorted(fs["upload_hour_kst_histogram"].items(), key=lambda kv: -kv[1])[:3]
        print(f"  업로드 집중 시간대(KST): {top_hours}")

    print("\n=== A. 소재 트렌드 (월별 상위 키워드 + 직전 4개월 대비) ===")
    for ym in months:
        if ym not in monthly_freq:
            continue
        trend = kw.detect_trend(monthly_freq, ym, baseline_months=4, min_frequency=20)
        top = kw.top_keywords(monthly_freq, ym, n=10, min_frequency=20)
        print(f"\n[{ym}] 상위 키워드(빈도>=20): {top}")
        risers = [r for r in trend if r["pct_change"] and r["pct_change"] > 0][:5]
        if risers:
            print("  상승 키워드:", [(r["keyword"], f"{r['pct_change']:+.0%}") for r in risers])


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--months", type=int, default=6, help="how many recent months to print")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    load_dotenv(ROOT / ".env")

    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        log.error("DATABASE_URL is not set")
        return 1

    conn = db.get_connection(database_url)
    videos = fetch_videos(conn)
    conn.close()
    log.info("loaded %d videos", len(videos))
    if not videos:
        log.error("no videos with published_at/duration_sec/title — nothing to analyze")
        return 1

    format_summary = run_format_trends(videos)
    monthly_freq = run_keyword_trends(videos)
    print_report(format_summary, monthly_freq, args.months)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
