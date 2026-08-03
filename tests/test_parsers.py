import pytest

from src.parsers import (
    is_short_candidate,
    now_utc_iso,
    parse_iso8601_duration,
    parse_utc,
    utc_to_kst,
)


class TestParseIso8601Duration:
    def test_hours_minutes_seconds(self):
        assert parse_iso8601_duration("PT1H2M3S") == 3723

    def test_seconds_only(self):
        assert parse_iso8601_duration("PT45S") == 45

    def test_minutes_only(self):
        assert parse_iso8601_duration("PT10M") == 600

    def test_hours_only(self):
        assert parse_iso8601_duration("PT2H") == 7200

    def test_zero_seconds(self):
        assert parse_iso8601_duration("PT0S") == 0

    def test_days_and_time(self):
        assert parse_iso8601_duration("P1DT2H") == 86400 + 7200

    def test_empty_string_returns_zero(self):
        assert parse_iso8601_duration("") == 0

    def test_none_returns_zero(self):
        assert parse_iso8601_duration(None) == 0

    def test_invalid_format_raises(self):
        with pytest.raises(ValueError):
            parse_iso8601_duration("not-a-duration")

    def test_bare_p_raises(self):
        with pytest.raises(ValueError):
            parse_iso8601_duration("P")


class TestShortCandidate:
    def test_under_threshold(self):
        assert is_short_candidate(179) is True

    def test_at_threshold(self):
        assert is_short_candidate(180) is True

    def test_over_threshold(self):
        assert is_short_candidate(181) is False

    def test_custom_threshold(self):
        assert is_short_candidate(200, max_duration_sec=300) is True


class TestUtcKstConversion:
    def test_basic_offset(self):
        kst = utc_to_kst("2026-01-01T00:00:00Z")
        assert kst.hour == 9
        assert kst.day == 1

    def test_midnight_crossing_forward(self):
        # 16:00 UTC -> 01:00 KST next day
        kst = utc_to_kst("2026-01-01T16:00:00Z")
        assert kst.day == 2
        assert kst.hour == 1

    def test_accepts_offset_suffix(self):
        kst = utc_to_kst("2026-01-01T00:00:00+00:00")
        assert kst.hour == 9

    def test_accepts_datetime_object(self):
        dt = parse_utc("2026-06-15T12:00:00Z")
        kst = utc_to_kst(dt)
        assert kst.hour == 21

    def test_roundtrip_preserves_instant(self):
        original = parse_utc("2026-03-10T05:30:00Z")
        kst = utc_to_kst(original)
        assert kst.astimezone(original.tzinfo) == original

    def test_rejects_unsupported_type(self):
        with pytest.raises(TypeError):
            utc_to_kst(12345)


def test_now_utc_iso_is_parseable():
    value = now_utc_iso()
    parsed = parse_utc(value)
    assert parsed.tzinfo is not None
