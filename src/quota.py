"""YouTube Data API quota estimation and daily budget allocation.

Unit costs are NOT hardcoded here — callers must pass values confirmed
against the official quota documentation (see docs/trending-collector-spec.md
section 10). This module only encodes the call-count model.
"""
import math


def estimate_daily_quota(n_categories: int, runs_per_day: int, avg_pages: int, unit_cost: int) -> int:
    """Trending collector: (categories + 1 for 'all') x runs/day x pages x unit cost."""
    calls_per_run = (n_categories + 1) * avg_pages
    return calls_per_run * runs_per_day * unit_cost


def estimate_channel_incremental_quota(
    n_channels: int,
    avg_growth_window_videos: int,
    unit_cost_channels_list: int,
    unit_cost_playlist_items: int,
    unit_cost_videos_list: int,
    batch_size: int = 50,
) -> int:
    """Daily incremental cost: channel snapshot + new-video detection + growth-window stats.

    channels.list and videos.list batch up to `batch_size` ids per call;
    playlistItems.list does not batch (one call per channel).
    """
    channels_list_cost = math.ceil(n_channels / batch_size) * unit_cost_channels_list
    playlist_cost = n_channels * unit_cost_playlist_items
    videos_cost = math.ceil(avg_growth_window_videos / batch_size) * unit_cost_videos_list
    return channels_list_cost + playlist_cost + videos_cost


def estimate_channel_backfill_quota(
    n_pending: int,
    per_channel_pages: int,
    unit_cost_playlist_items: int,
    unit_cost_videos_list: int,
) -> int:
    """One-time backfill cost: per_channel_pages of playlistItems + matching videos.list batches."""
    per_channel_cost = per_channel_pages * (unit_cost_playlist_items + unit_cost_videos_list)
    return n_pending * per_channel_cost


def allocate_daily_budget(daily_cap: int, trending_reserved: int, incremental_reserved: int) -> int:
    """Quota left over for backfill after trending + incremental reservations."""
    return max(daily_cap - trending_reserved - incremental_reserved, 0)


def backfill_slots_today(
    remaining_budget: int,
    per_channel_pages: int,
    unit_cost_playlist_items: int,
    unit_cost_videos_list: int,
) -> int:
    """How many channels can be fully backfilled today with the remaining budget."""
    per_channel_cost = per_channel_pages * (unit_cost_playlist_items + unit_cost_videos_list)
    if per_channel_cost <= 0:
        return 0
    return remaining_budget // per_channel_cost
