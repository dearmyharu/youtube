import sqlite3

import pytest

from src import db


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    db.init_db(c)
    yield c
    c.close()


def make_video(video_id="v1", channel_id="c1", first_seen_at="2026-01-01T00:00:00+00:00", title="Title A"):
    return {
        "video_id": video_id,
        "channel_id": channel_id,
        "title": title,
        "published_at": "2026-01-01T00:00:00+00:00",
        "duration_sec": 120,
        "category_id": "24",
        "tags": "[]",
        "thumbnail_url": "http://example.com/a.jpg",
        "first_seen_at": first_seen_at,
        "last_meta_at": first_seen_at,
    }


class TestSchema:
    def test_all_core_tables_exist(self, conn):
        tables = {r["name"] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        expected = {
            "runs", "videos", "video_meta_history", "video_stats", "trending_rank",
            "channel_pool", "channel_stats", "channel_collection_state",
        }
        assert expected.issubset(tables)

    def test_channel_trending_days_view_exists(self, conn):
        views = {r["name"] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='view'"
        ).fetchall()}
        assert "channel_trending_days" in views


class TestRuns:
    def test_start_and_finish_run(self, conn):
        db.start_run(conn, "run1", "trending", "2026-01-01T00:00:00+00:00")
        row = conn.execute("SELECT * FROM runs WHERE run_id='run1'").fetchone()
        assert row["status"] == "running"

        db.finish_run(conn, "run1", "2026-01-01T00:05:00+00:00", "success", 42, 10)
        row = conn.execute("SELECT * FROM runs WHERE run_id='run1'").fetchone()
        assert row["status"] == "success"
        assert row["quota_used"] == 42
        assert row["items_seen"] == 10


class TestVideoUpsertIdempotency:
    def test_first_seen_at_is_not_overwritten(self, conn):
        db.upsert_video(conn, make_video(first_seen_at="2026-01-01T00:00:00+00:00", title="Original"))
        db.upsert_video(conn, make_video(first_seen_at="2026-06-01T00:00:00+00:00", title="Updated"))
        row = conn.execute("SELECT * FROM videos WHERE video_id='v1'").fetchone()
        assert row["first_seen_at"] == "2026-01-01T00:00:00+00:00"
        assert row["title"] == "Updated"

    def test_rerun_same_run_produces_no_duplicate_stats(self, conn):
        stats = {
            "video_id": "v1", "collected_at": "2026-01-01T00:00:00+00:00", "run_id": "run1",
            "view_count": 100, "like_count": 10, "comment_count": 1,
        }
        db.insert_video_stats(conn, stats)
        db.insert_video_stats(conn, stats)
        count = conn.execute("SELECT COUNT(*) AS n FROM video_stats WHERE video_id='v1'").fetchone()["n"]
        assert count == 1

    def test_rerun_same_run_produces_no_duplicate_rank(self, conn):
        rank_row = {
            "collected_at": "2026-01-01T00:00:00+00:00", "run_id": "run1",
            "region": "KR", "category_id": "all", "rank": 1, "video_id": "v1",
        }
        db.insert_trending_rank(conn, rank_row)
        db.insert_trending_rank(conn, rank_row)
        count = conn.execute("SELECT COUNT(*) AS n FROM trending_rank").fetchone()["n"]
        assert count == 1

    def test_different_collected_at_creates_new_row(self, conn):
        db.insert_video_stats(conn, {
            "video_id": "v1", "collected_at": "2026-01-01T00:00:00+00:00", "run_id": "run1",
            "view_count": 100, "like_count": 10, "comment_count": 1,
        })
        db.insert_video_stats(conn, {
            "video_id": "v1", "collected_at": "2026-01-01T06:00:00+00:00", "run_id": "run2",
            "view_count": 200, "like_count": 20, "comment_count": 2,
        })
        count = conn.execute("SELECT COUNT(*) AS n FROM video_stats WHERE video_id='v1'").fetchone()["n"]
        assert count == 2


class TestChannelPool:
    def test_upsert_ignores_existing_manual_decision(self, conn):
        db.upsert_channel_pool(conn, {
            "channel_id": "c1", "channel_title": "Channel", "first_seen_at": "2026-01-01T00:00:00+00:00",
            "source": "trending", "screened_at": None, "passed_filter": None,
            "group_type": None, "decision": "pending", "decision_reason": None,
        })
        db.update_channel_decision(conn, "c1", "include", "core comparison channel", group_type="개인")
        # re-discovering the same channel from trending should not reset the decision
        db.upsert_channel_pool(conn, {
            "channel_id": "c1", "channel_title": "Channel", "first_seen_at": "2026-02-01T00:00:00+00:00",
            "source": "trending", "screened_at": None, "passed_filter": None,
            "group_type": None, "decision": "pending", "decision_reason": None,
        })
        row = conn.execute("SELECT * FROM channel_pool WHERE channel_id='c1'").fetchone()
        assert row["decision"] == "include"
        assert row["first_seen_at"] == "2026-01-01T00:00:00+00:00"

    def test_default_tier_is_panel(self, conn):
        db.upsert_channel_pool(conn, {
            "channel_id": "c1", "channel_title": "Channel", "first_seen_at": "2026-01-01T00:00:00+00:00",
            "source": "trending", "screened_at": None, "passed_filter": None,
            "group_type": None, "decision": "pending", "decision_reason": None,
        })
        row = conn.execute("SELECT tier FROM channel_pool WHERE channel_id='c1'").fetchone()
        assert row["tier"] == "panel"

    def test_get_included_channels_filters_by_decision(self, conn):
        for cid, decision in [("c1", "include"), ("c2", "exclude"), ("c3", "pending")]:
            db.upsert_channel_pool(conn, {
                "channel_id": cid, "channel_title": cid, "first_seen_at": "2026-01-01T00:00:00+00:00",
                "source": "trending", "screened_at": None, "passed_filter": None,
                "group_type": None, "decision": decision, "decision_reason": None,
            })
        included = db.get_included_channels(conn)
        assert {c["channel_id"] for c in included} == {"c1"}


class TestBackfillQueue:
    def _add_channel(self, conn, channel_id, tier, first_seen_at, decision="include"):
        db.upsert_channel_pool(conn, {
            "channel_id": channel_id, "channel_title": channel_id, "first_seen_at": first_seen_at,
            "source": "manual", "screened_at": None, "passed_filter": None,
            "group_type": None, "decision": decision, "decision_reason": None, "tier": tier,
        })

    def test_core_channels_come_before_panel(self, conn):
        self._add_channel(conn, "panel1", "panel", "2026-01-01T00:00:00+00:00")
        self._add_channel(conn, "core1", "core", "2026-01-02T00:00:00+00:00")
        queue = db.get_backfill_queue(conn)
        assert [c["channel_id"] for c in queue] == ["core1", "panel1"]

    def test_panel_is_fifo_by_first_seen_at(self, conn):
        self._add_channel(conn, "panel2", "panel", "2026-01-05T00:00:00+00:00")
        self._add_channel(conn, "panel1", "panel", "2026-01-01T00:00:00+00:00")
        queue = db.get_backfill_queue(conn)
        assert [c["channel_id"] for c in queue] == ["panel1", "panel2"]

    def test_done_channels_are_excluded(self, conn):
        self._add_channel(conn, "c1", "panel", "2026-01-01T00:00:00+00:00")
        db.upsert_channel_collection_state(conn, "c1", backfill_status="done")
        queue = db.get_backfill_queue(conn)
        assert queue == []

    def test_failed_channels_are_retried(self, conn):
        self._add_channel(conn, "c1", "panel", "2026-01-01T00:00:00+00:00")
        db.upsert_channel_collection_state(conn, "c1", backfill_status="failed")
        queue = db.get_backfill_queue(conn)
        assert [c["channel_id"] for c in queue] == ["c1"]

    def test_excluded_channels_never_queued(self, conn):
        self._add_channel(conn, "c1", "panel", "2026-01-01T00:00:00+00:00", decision="exclude")
        queue = db.get_backfill_queue(conn)
        assert queue == []


class TestChannelCollectionState:
    def test_upsert_creates_then_updates(self, conn):
        db.upsert_channel_collection_state(conn, "c1", backfill_status="pending")
        row = conn.execute("SELECT * FROM channel_collection_state WHERE channel_id='c1'").fetchone()
        assert row["backfill_status"] == "pending"

        db.upsert_channel_collection_state(conn, "c1", backfill_status="done", backfill_completed_at="2026-01-02T00:00:00+00:00")
        row = conn.execute("SELECT * FROM channel_collection_state WHERE channel_id='c1'").fetchone()
        assert row["backfill_status"] == "done"
        assert row["backfill_completed_at"] == "2026-01-02T00:00:00+00:00"


class TestChannelStatsAppendOnly:
    def test_same_channel_and_time_is_idempotent(self, conn):
        stats = {
            "channel_id": "c1", "collected_at": "2026-01-01T00:00:00+00:00", "run_id": "run1",
            "subscriber_count": 1000, "view_count": 5000, "video_count": 10,
        }
        db.insert_channel_stats(conn, stats)
        db.insert_channel_stats(conn, stats)
        count = conn.execute("SELECT COUNT(*) AS n FROM channel_stats WHERE channel_id='c1'").fetchone()["n"]
        assert count == 1

    def test_different_day_appends_new_row(self, conn):
        db.insert_channel_stats(conn, {
            "channel_id": "c1", "collected_at": "2026-01-01T00:00:00+00:00", "run_id": "run1",
            "subscriber_count": 1000, "view_count": 5000, "video_count": 10,
        })
        db.insert_channel_stats(conn, {
            "channel_id": "c1", "collected_at": "2026-01-02T00:00:00+00:00", "run_id": "run2",
            "subscriber_count": 1010, "view_count": 5100, "video_count": 10,
        })
        count = conn.execute("SELECT COUNT(*) AS n FROM channel_stats WHERE channel_id='c1'").fetchone()["n"]
        assert count == 2


class TestGrowthWindowVideoIds:
    def test_boundary_at_exactly_window_days(self, conn):
        db.upsert_video(conn, make_video(video_id="old"))
        # manually set published_at to control the boundary precisely
        conn.execute(
            "UPDATE videos SET published_at = datetime('now', '-7 days') WHERE video_id='old'"
        )
        db.upsert_video(conn, make_video(video_id="new"))
        conn.execute(
            "UPDATE videos SET published_at = datetime('now', '-1 days') WHERE video_id='new'"
        )

        ids = db.get_growth_window_video_ids(conn, ["c1"], 7)
        assert "new" in ids
        assert "old" in ids  # exactly 7 days ago is still within the window (>=)

    def test_outside_window_excluded(self, conn):
        db.upsert_video(conn, make_video(video_id="ancient"))
        conn.execute(
            "UPDATE videos SET published_at = datetime('now', '-30 days') WHERE video_id='ancient'"
        )
        ids = db.get_growth_window_video_ids(conn, ["c1"], 7)
        assert "ancient" not in ids

    def test_empty_channel_list_returns_empty(self, conn):
        assert db.get_growth_window_video_ids(conn, [], 7) == []
