from src.analysis.keywords import (
    clean_title,
    detect_trend,
    filter_tokens,
    find_channel_fixed_phrases,
    is_series_notation,
    monthly_keyword_frequency,
    strip_channel_phrases,
    top_keywords,
)


class TestCleanTitle:
    def test_strips_urls(self):
        assert clean_title("먹방 영상 https://youtu.be/abc 입니다") == "먹방 영상 입니다"

    def test_collapses_whitespace(self):
        assert clean_title("여행   브이로그\n\n제주도") == "여행 브이로그 제주도"

    def test_empty_input(self):
        assert clean_title("") == ""
        assert clean_title(None) == ""


class TestIsSeriesNotation:
    def test_ep_dot_number(self):
        assert is_series_notation("무한도전 EP.42") is True

    def test_korean_hoa(self):
        assert is_series_notation("우리 결혼했어요 12화") is True

    def test_korean_pyeon(self):
        assert is_series_notation("여행기 3편") is True

    def test_hashtag_number(self):
        assert is_series_notation("데일리룩 #7") is True

    def test_no_series_marker(self):
        assert is_series_notation("오늘의 브이로그") is False


class TestFilterTokens:
    def test_drops_stopwords(self):
        assert filter_tokens(["오늘", "여행", "채널", "제주도"]) == ["여행", "제주도"]

    def test_keeps_genre_words(self):
        # topic/genre words must survive — they're the signal a topic-trend report wants
        assert filter_tokens(["브이로그", "먹방"]) == ["브이로그", "먹방"]

    def test_drops_short_tokens(self):
        assert filter_tokens(["가", "제주도", "안"]) == ["제주도"]

    def test_drops_pure_numeric(self):
        assert filter_tokens(["2024", "제주도", "42"]) == ["제주도"]

    def test_drops_reaction_jamo(self):
        assert filter_tokens(["ㅋㅋㅋ", "ㄷㄷ", "ㅠㅠㅠ", "제주도"]) == ["제주도"]

    def test_drops_calendar_labels(self):
        assert filter_tokens(["4월", "25일", "2026년", "제주도"]) == ["제주도"]

    def test_keeps_meaningful_tokens(self):
        assert filter_tokens(["제주도", "맛집", "투어"]) == ["제주도", "맛집", "투어"]

    def test_respects_custom_min_len(self):
        assert filter_tokens(["제주도", "AB"], min_len=3) == ["제주도"]


class TestChannelFixedPhrases:
    def test_detects_phrase_in_most_videos(self):
        channel_tokens = {
            "chA": [["십오야", "여행"], ["십오야", "맛집"], ["십오야", "게임"], ["십오야", "낚시"]],
        }
        fixed = find_channel_fixed_phrases(channel_tokens, min_videos=4, min_ratio=0.5)
        assert fixed["chA"] == {"십오야"}

    def test_below_ratio_not_flagged(self):
        channel_tokens = {
            "chA": [["십오야", "여행"], ["맛집"], ["게임"], ["낚시"]],
        }
        fixed = find_channel_fixed_phrases(channel_tokens, min_videos=4, min_ratio=0.5)
        assert fixed.get("chA", set()) == set()

    def test_below_min_videos_skipped_entirely(self):
        channel_tokens = {"chA": [["십오야", "여행"], ["십오야", "맛집"]]}
        fixed = find_channel_fixed_phrases(channel_tokens, min_videos=10, min_ratio=0.5)
        assert "chA" not in fixed

    def test_strip_channel_phrases(self):
        assert strip_channel_phrases(["십오야", "여행", "맛집"], {"십오야"}) == ["여행", "맛집"]

    def test_strip_with_no_phrases(self):
        assert strip_channel_phrases(["여행", "맛집"], set()) == ["여행", "맛집"]


class TestMonthlyKeywordFrequency:
    def test_counts_once_per_video_not_per_occurrence(self):
        records = [("2026-01", ["맛집", "맛집", "여행"])]
        freq = monthly_keyword_frequency(records)
        assert freq["2026-01"]["맛집"] == 1
        assert freq["2026-01"]["여행"] == 1

    def test_aggregates_across_videos(self):
        records = [
            ("2026-01", ["맛집", "여행"]),
            ("2026-01", ["맛집", "게임"]),
            ("2026-02", ["맛집"]),
        ]
        freq = monthly_keyword_frequency(records)
        assert freq["2026-01"]["맛집"] == 2
        assert freq["2026-01"]["여행"] == 1
        assert freq["2026-02"]["맛집"] == 1


class TestTopKeywords:
    def test_filters_by_min_frequency_and_sorts(self):
        monthly_freq = {"2026-01": {"맛집": 25, "여행": 19, "게임": 30}}
        top = top_keywords(monthly_freq, "2026-01", n=20, min_frequency=20)
        assert top == [("게임", 30), ("맛집", 25)]

    def test_missing_month_returns_empty(self):
        assert top_keywords({}, "2026-01") == []

    def test_respects_n_limit(self):
        monthly_freq = {"2026-01": {f"kw{i}": 100 - i for i in range(30)}}
        top = top_keywords(monthly_freq, "2026-01", n=5, min_frequency=0)
        assert len(top) == 5
        assert top[0][0] == "kw0"


class TestDetectTrend:
    def test_flags_new_keyword(self):
        monthly_freq = {
            "2025-11": {"먹방": 50},
            "2025-12": {"먹방": 50},
            "2026-01": {"먹방": 50, "챌린지": 25},
        }
        results = detect_trend(monthly_freq, "2026-01", baseline_months=4, min_frequency=20)
        chall = next(r for r in results if r["keyword"] == "챌린지")
        assert chall["is_new"] is True
        assert chall["pct_change"] is None

    def test_computes_pct_change_against_trailing_average(self):
        monthly_freq = {
            "2025-10": {"먹방": 20},
            "2025-11": {"먹방": 30},
            "2025-12": {"먹방": 40},
            "2026-01": {"먹방": 60},
        }
        results = detect_trend(monthly_freq, "2026-01", baseline_months=3, min_frequency=20)
        r = results[0]
        assert r["keyword"] == "먹방"
        assert r["baseline"] == 30  # avg(20,30,40)
        assert r["pct_change"] == 1.0  # (60-30)/30

    def test_below_min_frequency_excluded(self):
        monthly_freq = {"2025-12": {"먹방": 100}, "2026-01": {"먹방": 15}}
        results = detect_trend(monthly_freq, "2026-01", min_frequency=20)
        assert results == []

    def test_unknown_month_returns_empty(self):
        assert detect_trend({"2026-01": {"먹방": 50}}, "2026-02") == []

    def test_sorted_by_pct_change_descending(self):
        monthly_freq = {
            "2025-12": {"a": 20, "b": 20},
            "2026-01": {"a": 40, "b": 100},
        }
        results = detect_trend(monthly_freq, "2026-01", baseline_months=1, min_frequency=20)
        assert [r["keyword"] for r in results] == ["b", "a"]
