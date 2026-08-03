from src.quota import (
    allocate_daily_budget,
    backfill_slots_today,
    estimate_channel_backfill_quota,
    estimate_channel_incremental_quota,
    estimate_daily_quota,
)


class TestEstimateDailyQuota:
    def test_basic(self):
        # 8 categories + 'all' = 9 charts, 3 runs/day, 1 page, unit cost 1
        assert estimate_daily_quota(8, 3, 1, 1) == 27

    def test_zero_categories_still_counts_all_chart(self):
        assert estimate_daily_quota(0, 3, 1, 1) == 3

    def test_scales_with_pages_and_unit_cost(self):
        assert estimate_daily_quota(1, 2, 3, 5) == (1 + 1) * 3 * 5 * 2


class TestEstimateChannelIncrementalQuota:
    def test_matches_manual_calc(self):
        # 100 channels -> 2 channels.list batches, 100 playlistItems calls,
        # 200 growth-window videos -> 4 videos.list batches
        result = estimate_channel_incremental_quota(
            n_channels=100, avg_growth_window_videos=200,
            unit_cost_channels_list=1, unit_cost_playlist_items=1, unit_cost_videos_list=1,
        )
        assert result == 2 + 100 + 4

    def test_batching_reduces_channels_list_cost(self):
        result = estimate_channel_incremental_quota(
            n_channels=50, avg_growth_window_videos=50,
            unit_cost_channels_list=1, unit_cost_playlist_items=1, unit_cost_videos_list=1,
        )
        # exactly one batch each for channels.list and videos.list
        assert result == 1 + 50 + 1


class TestEstimateChannelBackfillQuota:
    def test_matches_confirmed_design_estimate(self):
        # 500 videos / 50 batch size = 10 pages -> ~20 units/channel
        cost = estimate_channel_backfill_quota(
            n_pending=1, per_channel_pages=10,
            unit_cost_playlist_items=1, unit_cost_videos_list=1,
        )
        assert cost == 20

    def test_scales_with_pending_channels(self):
        cost = estimate_channel_backfill_quota(
            n_pending=20, per_channel_pages=10,
            unit_cost_playlist_items=1, unit_cost_videos_list=1,
        )
        assert cost == 400


class TestAllocateDailyBudget:
    def test_basic_subtraction(self):
        assert allocate_daily_budget(10000, 200, 3000) == 6800

    def test_never_negative(self):
        assert allocate_daily_budget(1000, 800, 800) == 0


class TestBackfillSlotsToday:
    def test_basic(self):
        # per-channel cost = 10 * (1+1) = 20, budget 6800 -> 340 channels
        assert backfill_slots_today(6800, 10, 1, 1) == 340

    def test_zero_budget(self):
        assert backfill_slots_today(0, 10, 1, 1) == 0

    def test_zero_per_channel_cost_is_safe(self):
        assert backfill_slots_today(1000, 0, 0, 0) == 0
