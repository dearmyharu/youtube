"""media-insight dashboard — Streamlit.

Three layers:
  1. Overview   — freshness, channel/backfill counts, this month vs trailing baseline, top movers
  2. Diagnosis  — tabs: A(소재) / B(형식) / 채널 비교
  (3. Action is intentionally omitted for v1 — see docs/system-guide.md: the trend
     baselines are still too shallow for confident recommendations)

Reads only from precomputed aggregate tables (monthly_keyword_trend,
monthly_format_trend) plus a few small live queries — see dashboard/queries.py.
Run `python -m src.build_dashboard_aggregates` to refresh the aggregates.
"""
import json
import os
import sys
from pathlib import Path

import altair as alt
import pandas as pd
import streamlit as st

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

from src import db
from src.analysis.keywords import detect_trend
from src.storage_client import SupabaseStorageClient
from dashboard import queries

st.set_page_config(page_title="media-insight", page_icon="📺", layout="wide")

# -- deliberate palette: blue/orange categorical pair (CVD-reasonable), status colors
# kept separate from the categorical hues, one sequential blue for magnitude bars.
LONGFORM_COLOR = "#2E6E8E"
SHORTS_COLOR = "#D97B29"
GOOD_COLOR = "#3A9D5D"
BAD_COLOR = "#C0392B"
MUTED_COLOR = "#8A8F98"
SEQUENTIAL_SCHEME = "blues"


def database_url() -> str:
    if "DATABASE_URL" in st.secrets:
        return st.secrets["DATABASE_URL"]
    return os.environ["DATABASE_URL"]


def thumbnail_public_url(thumbnail_path: str) -> str:
    # public_url() is a pure string join, no auth needed, so a dummy service key is fine here
    project_url = st.secrets["SUPABASE_URL"] if "SUPABASE_URL" in st.secrets else os.environ["SUPABASE_URL"]
    return SupabaseStorageClient(project_url, "unused").public_url(thumbnail_path)


@st.cache_data(ttl=600)
def load_freshness() -> pd.DataFrame:
    conn = db.get_connection(database_url())
    try:
        return queries.get_job_freshness(conn)
    finally:
        conn.close()


@st.cache_data(ttl=600)
def load_channel_counts() -> dict:
    conn = db.get_connection(database_url())
    try:
        return queries.get_channel_counts(conn)
    finally:
        conn.close()


@st.cache_data(ttl=600)
def load_format_trend() -> pd.DataFrame:
    conn = db.get_connection(database_url())
    try:
        return queries.get_monthly_format_trend(conn)
    finally:
        conn.close()


@st.cache_data(ttl=600)
def load_keyword_trend_dict(months: int) -> dict:
    conn = db.get_connection(database_url())
    try:
        return queries.get_monthly_keyword_trend_dict(conn, months=months)
    finally:
        conn.close()


@st.cache_data(ttl=600)
def load_channel_leaderboard(limit: int) -> pd.DataFrame:
    conn = db.get_connection(database_url())
    try:
        return queries.get_channel_leaderboard(conn, limit=limit)
    finally:
        conn.close()


@st.cache_data(ttl=600)
def load_recent_thumbnails(limit: int) -> pd.DataFrame:
    conn = db.get_connection(database_url())
    try:
        return queries.get_recent_thumbnails(conn, limit=limit)
    finally:
        conn.close()


def fmt_ago(ts) -> str:
    if pd.isna(ts):
        return "기록 없음"
    delta = pd.Timestamp.now(tz="UTC") - pd.Timestamp(ts)
    hours = delta.total_seconds() / 3600
    if hours < 1:
        return f"{int(delta.total_seconds() / 60)}분 전"
    if hours < 48:
        return f"{hours:.1f}시간 전"
    return f"{hours / 24:.1f}일 전"


# ============================================================ Layer 1: Overview

st.title("media-insight")
st.caption("YouTube 크리에이터 채널 트렌드 대시보드 · 소재(A) · 형식(B) · 채널 비교")

st.header("1. 개요")

freshness = load_freshness()
counts = load_channel_counts()
format_df = load_format_trend()

freshness_cols = st.columns(max(len(freshness), 1))
job_labels = {
    "trending": "트렌딩",
    "channel_backfill": "채널 백필",
    "channel_incremental": "채널 증분",
    "channel_discovery": "채널 발견",
}
for col, (_, row) in zip(freshness_cols, freshness.iterrows()):
    label = job_labels.get(row["job_name"], row["job_name"])
    ok = row["status"] == "success"
    pill = "🟢 정상" if ok else ("🟡 진행중" if row["status"] == "running" else "🔴 실패")
    with col:
        st.metric(label, pill, help=f"마지막 실행: {fmt_ago(row['started_at'])}")

st.divider()

kpi_cols = st.columns(4)
kpi_cols[0].metric("추적 채널", f"{counts['included']:,}", help=f"뉴스 카테고리 등 제외 {counts['excluded']:,}개")
kpi_cols[1].metric("백필 완료", f"{counts['backfill_done']:,}",
                    delta=f"실패 {counts['backfill_failed']}건" if counts["backfill_failed"] else None,
                    delta_color="inverse")

if not format_df.empty:
    recent = format_df.tail(24).reset_index(drop=True)  # last 24 months only — older rows are sparse legacy uploads
    latest = recent.iloc[-1]
    baseline = recent.iloc[-5:-1] if len(recent) >= 5 else recent.iloc[:-1]
    baseline_videos = baseline["video_count"].mean() if len(baseline) else None
    baseline_shorts_ratio = baseline["shorts_ratio"].mean() if len(baseline) else None

    kpi_cols[2].metric(
        f"{latest['year_month']} 신규 영상",
        f"{int(latest['video_count']):,}",
        delta=None if baseline_videos in (None, 0) else f"{(latest['video_count'] / baseline_videos - 1):+.0%} (직전 4개월 대비)",
    )
    kpi_cols[3].metric(
        "쇼츠 비중",
        f"{latest['shorts_ratio']:.0%}",
        delta=None if baseline_shorts_ratio is None else f"{(latest['shorts_ratio'] - baseline_shorts_ratio):+.1%}p (직전 4개월 대비)",
    )

st.subheader("급상승 · 급하락 키워드")
kw_freq = load_keyword_trend_dict(months=6)
if kw_freq:
    latest_month = max(kw_freq.keys())
    trend = detect_trend(kw_freq, latest_month, baseline_months=4, min_frequency=20)
    risers = [r for r in trend if r["pct_change"] and r["pct_change"] > 0][:3]
    fallers = [r for r in trend if r["pct_change"] and r["pct_change"] < 0][-3:]

    rc, fc = st.columns(2)
    with rc:
        st.markdown(f"**상승 ({latest_month})**")
        for r in risers:
            st.markdown(f"- `{r['keyword']}` — {r['current']}건 ({r['pct_change']:+.0%})")
        if not risers:
            st.caption("이번 달 기준을 만족하는 상승 키워드 없음")
    with fc:
        st.markdown(f"**하락 ({latest_month})**")
        for r in fallers:
            st.markdown(f"- `{r['keyword']}` — {r['current']}건 ({r['pct_change']:+.0%})")
        if not fallers:
            st.caption("이번 달 기준을 만족하는 하락 키워드 없음")
else:
    st.info("아직 monthly_keyword_trend가 비어 있습니다. `python -m src.build_dashboard_aggregates`를 먼저 실행하세요.")

st.divider()

# ============================================================ Layer 2: Diagnosis

st.header("2. 진단")
tab_a, tab_b, tab_channels, tab_thumbs = st.tabs(["소재 (A)", "형식 (B)", "채널 비교", "썸네일"])

with tab_a:
    if not kw_freq:
        st.info("데이터 없음")
    else:
        months_sorted = sorted(kw_freq.keys())
        selected_month = st.selectbox("월 선택", months_sorted[::-1], index=0)
        top_n = pd.DataFrame(
            sorted(kw_freq[selected_month].items(), key=lambda kv: -kv[1])[:20],
            columns=["keyword", "video_count"],
        )
        if top_n.empty:
            st.caption("이 달은 최소 빈도(5건) 이상인 키워드가 없습니다.")
        else:
            chart = alt.Chart(top_n).mark_bar(color=LONGFORM_COLOR, cornerRadiusEnd=4).encode(
                x=alt.X("video_count:Q", title="영상 수"),
                y=alt.Y("keyword:N", sort="-x", title=None),
                tooltip=["keyword", "video_count"],
            ).properties(height=28 * len(top_n), title=f"{selected_month} 상위 키워드")
            st.altair_chart(chart, use_container_width=True)

        st.markdown("**직전 4개월 대비 상승/하락** (월 20건 이상 등장한 키워드만)")
        trend_rows = detect_trend(kw_freq, selected_month, baseline_months=4, min_frequency=20)
        if trend_rows:
            trend_df = pd.DataFrame(trend_rows)[["keyword", "current", "baseline", "pct_change", "is_new"]]
            trend_df["baseline"] = trend_df["baseline"].round(1)
            trend_df["pct_change"] = trend_df["pct_change"].apply(lambda v: None if v is None else round(v * 100, 1))
            st.dataframe(trend_df, use_container_width=True, hide_index=True)
        else:
            st.caption("조건을 만족하는 키워드가 없습니다.")

with tab_b:
    if format_df.empty:
        st.info("데이터 없음")
    else:
        recent = format_df.tail(24).copy()

        st.markdown("**쇼츠 비중 추이**")
        ratio_chart = alt.Chart(recent).mark_line(color=SHORTS_COLOR, point=True).encode(
            x=alt.X("year_month:N", title=None),
            y=alt.Y("shorts_ratio:Q", title="쇼츠 비중", axis=alt.Axis(format="%")),
            tooltip=["year_month", alt.Tooltip("shorts_ratio:Q", format=".0%")],
        ).properties(height=220)
        st.altair_chart(ratio_chart, use_container_width=True)

        st.markdown("**평균 영상 길이 추이** (롱폼/쇼츠는 스케일이 달라 분리)")
        c1, c2 = st.columns(2)
        with c1:
            longform_chart = alt.Chart(recent).transform_calculate(
                minutes="datum.avg_duration_longform_sec / 60"
            ).mark_line(color=LONGFORM_COLOR, point=True).encode(
                x=alt.X("year_month:N", title=None),
                y=alt.Y("minutes:Q", title="분"),
                tooltip=["year_month", alt.Tooltip("minutes:Q", format=".1f")],
            ).properties(height=200, title="롱폼 평균 길이(분)")
            st.altair_chart(longform_chart, use_container_width=True)
        with c2:
            shorts_chart = alt.Chart(recent).mark_line(color=SHORTS_COLOR, point=True).encode(
                x=alt.X("year_month:N", title=None),
                y=alt.Y("avg_duration_shorts_sec:Q", title="초"),
                tooltip=["year_month", alt.Tooltip("avg_duration_shorts_sec:Q", format=".0f")],
            ).properties(height=200, title="쇼츠 평균 길이(초)")
            st.altair_chart(shorts_chart, use_container_width=True)

        st.markdown("**업로드 요일 × 시간대** (최근 달, KST)")
        latest_row = recent.iloc[-1]
        hour_hist = latest_row["upload_hour_histogram"]
        weekday_hist = latest_row["upload_weekday_histogram"]
        weekday_names = ["월", "화", "수", "목", "금", "토", "일"]

        heat_rows = []
        # only hour granularity is stored per month; weekday is a separate 1-D histogram,
        # so show them as two bars rather than fabricating a joint hour×weekday grid
        for h in range(24):
            heat_rows.append({"시간": h, "건수": hour_hist.get(str(h), 0)})
        hour_df = pd.DataFrame(heat_rows)
        hour_chart = alt.Chart(hour_df).mark_bar(color=LONGFORM_COLOR, cornerRadiusTopLeft=3, cornerRadiusTopRight=3).encode(
            x=alt.X("시간:O", title="시간대(KST)"),
            y=alt.Y("건수:Q"),
            tooltip=["시간", "건수"],
        ).properties(height=200, title=f"{latest_row['year_month']} 업로드 시간대 분포")
        st.altair_chart(hour_chart, use_container_width=True)

        weekday_df = pd.DataFrame(
            [{"요일": weekday_names[int(k)], "건수": v} for k, v in weekday_hist.items()]
        )
        weekday_order = weekday_names
        weekday_chart = alt.Chart(weekday_df).mark_bar(color=SHORTS_COLOR, cornerRadiusTopLeft=3, cornerRadiusTopRight=3).encode(
            x=alt.X("요일:N", sort=weekday_order, title=None),
            y=alt.Y("건수:Q"),
            tooltip=["요일", "건수"],
        ).properties(height=200, title=f"{latest_row['year_month']} 업로드 요일 분포")
        st.altair_chart(weekday_chart, use_container_width=True)

        st.markdown("**시리즈 표기(EP./화/편) 사용 비율 추이**")
        series_chart = alt.Chart(recent).mark_line(color=MUTED_COLOR, point=True).encode(
            x=alt.X("year_month:N", title=None),
            y=alt.Y("series_notation_ratio:Q", title="비율", axis=alt.Axis(format="%")),
            tooltip=["year_month", alt.Tooltip("series_notation_ratio:Q", format=".1%")],
        ).properties(height=180)
        st.altair_chart(series_chart, use_container_width=True)

with tab_channels:
    st.caption("평균이 아니라 **중앙값** 기준입니다 — 조회수는 우편향 분포라 평균이 소수 영상에 쉽게 휘둘립니다. "
               "영상 5개 미만인 채널은 제외했습니다.")
    top_n_channels = st.slider("표시할 채널 수", 10, 50, 20)
    leaderboard = load_channel_leaderboard(limit=top_n_channels)
    if leaderboard.empty:
        st.info("데이터 없음")
    else:
        chart_df = leaderboard.head(15)
        bar = alt.Chart(chart_df).mark_bar(cornerRadiusEnd=4).encode(
            x=alt.X("median_views:Q", title="중앙값 조회수"),
            y=alt.Y("channel_title:N", sort="-x", title=None),
            color=alt.Color("tier:N", scale=alt.Scale(domain=["core", "panel"], range=[LONGFORM_COLOR, MUTED_COLOR]),
                             legend=alt.Legend(title="tier")),
            tooltip=["channel_title", "median_views", "video_count", "subscriber_count"],
        ).properties(height=28 * len(chart_df), title="중앙값 조회수 상위 채널 (n≥5)")
        st.altair_chart(bar, use_container_width=True)

        st.dataframe(
            leaderboard.rename(columns={
                "channel_title": "채널", "tier": "우선순위", "subscriber_count": "구독자",
                "median_views": "중앙값 조회수", "video_count": "수집 영상 수", "uploads_last_90d": "최근 90일 업로드",
            }),
            use_container_width=True, hide_index=True,
        )

with tab_thumbs:
    st.caption(
        "새로 발견되는 영상만 썸네일을 저장합니다(기존 대량 백로그는 소급 저장하지 않음) — "
        "매일 조금씩 늘어납니다. 발견 순 최신 정렬."
    )
    n_thumbs = st.slider("표시할 개수", 10, 120, 60, key="n_thumbs")
    thumbs = load_recent_thumbnails(limit=n_thumbs)
    if thumbs.empty:
        st.info("아직 저장된 썸네일이 없습니다.")
    else:
        cols = st.columns(5)
        for i, row in thumbs.iterrows():
            with cols[i % 5]:
                st.image(thumbnail_public_url(row["thumbnail_path"]), use_container_width=True)
                title = row["title"] or ""
                short_title = title[:40] + ("…" if len(title) > 40 else "")
                view_line = f"조회수 {int(row['view_count']):,}" if pd.notna(row["view_count"]) else "조회수 미집계"
                st.caption(f"**{row['channel_title']}**  \n{short_title}  \n{view_line}")

st.divider()
st.caption(
    "3층(편성 제안)은 아직 없습니다 — 트렌드 베이스라인이 몇 주 더 쌓인 뒤 추가 예정입니다. "
    "인기급상승 기반 데이터는 무작위 표본이 아니라 '노출에 성공한 콘텐츠'의 특성입니다."
)
