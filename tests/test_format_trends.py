from src.analysis.format_trends import monthly_format_summary


def make_row(published_at, duration_sec, title="영상 제목"):
    return {"published_at": published_at, "duration_sec": duration_sec, "title": title}


class TestMonthlyFormatSummary:
    def test_buckets_by_kst_month_not_utc_month(self):
        # 2026-01-31T16:00:00Z -> KST 2026-02-01T01:00:00 (crosses into next month)
        rows = [make_row("2026-01-31T16:00:00Z", 300)]
        summary = monthly_format_summary(rows)
        assert "2026-02" in summary
        assert "2026-01" not in summary

    def test_shorts_vs_longform_split(self):
        rows = [
            make_row("2026-01-05T00:00:00Z", 60),   # short
            make_row("2026-01-05T00:00:00Z", 180),  # short (boundary, <=180)
            make_row("2026-01-05T00:00:00Z", 181),  # longform
            make_row("2026-01-05T00:00:00Z", 600),  # longform
        ]
        summary = monthly_format_summary(rows)["2026-01"]
        assert summary["video_count"] == 4
        assert summary["shorts_count"] == 2
        assert summary["shorts_ratio"] == 0.5
        assert summary["avg_duration_shorts_sec"] == 120
        assert summary["avg_duration_longform_sec"] == (181 + 600) / 2

    def test_series_notation_ratio(self):
        rows = [
            make_row("2026-01-05T00:00:00Z", 300, title="여행기 EP.3"),
            make_row("2026-01-05T00:00:00Z", 300, title="그냥 브이로그"),
        ]
        summary = monthly_format_summary(rows)["2026-01"]
        assert summary["series_notation_ratio"] == 0.5

    def test_upload_hour_and_weekday_histograms_use_kst(self):
        # 2026-01-05T15:00:00Z (Monday) -> KST 2026-01-06T00:00:00 (Tuesday)
        rows = [make_row("2026-01-05T15:00:00Z", 300)]
        summary = monthly_format_summary(rows)["2026-01"]
        assert summary["upload_hour_kst_histogram"] == {0: 1}
        assert summary["upload_weekday_kst_histogram"] == {1: 1}  # Tuesday = 1

    def test_skips_rows_missing_required_fields(self):
        rows = [
            {"published_at": None, "duration_sec": 100, "title": "x"},
            {"published_at": "2026-01-05T00:00:00Z", "duration_sec": None, "title": "x"},
            make_row("2026-01-05T00:00:00Z", 100),
        ]
        summary = monthly_format_summary(rows)["2026-01"]
        assert summary["video_count"] == 1

    def test_empty_input(self):
        assert monthly_format_summary([]) == {}
