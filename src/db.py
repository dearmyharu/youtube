"""SQLite schema and access helpers, shared by the trending and channel collectors.

Design principles (see docs/trending-collector-spec.md #0):
- observation tables (video_stats, trending_rank, channel_stats) are append-only
- `videos` is upserted but `first_seen_at` is never overwritten
- `channel_collection_state` is the only table that is freely UPDATEd (bookkeeping, not an observation)
"""
import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
  run_id        TEXT PRIMARY KEY,
  job_name      TEXT NOT NULL,
  started_at    TEXT NOT NULL,
  finished_at   TEXT,
  status        TEXT,
  quota_used    INTEGER,
  items_seen    INTEGER,
  error_message TEXT
);

CREATE TABLE IF NOT EXISTS videos (
  video_id       TEXT PRIMARY KEY,
  channel_id     TEXT NOT NULL,
  title          TEXT,
  published_at   TEXT,
  duration_sec   INTEGER,
  category_id    TEXT,
  tags           TEXT,
  thumbnail_url  TEXT,
  first_seen_at  TEXT NOT NULL,
  last_meta_at   TEXT
);

CREATE TABLE IF NOT EXISTS video_meta_history (
  video_id      TEXT NOT NULL,
  observed_at   TEXT NOT NULL,
  title         TEXT,
  thumbnail_url TEXT,
  PRIMARY KEY (video_id, observed_at)
);

CREATE TABLE IF NOT EXISTS video_stats (
  video_id      TEXT NOT NULL,
  collected_at  TEXT NOT NULL,
  run_id        TEXT NOT NULL,
  view_count    INTEGER,
  like_count    INTEGER,
  comment_count INTEGER,
  PRIMARY KEY (video_id, collected_at)
);

CREATE TABLE IF NOT EXISTS trending_rank (
  collected_at TEXT NOT NULL,
  run_id       TEXT NOT NULL,
  region       TEXT NOT NULL DEFAULT 'KR',
  category_id  TEXT NOT NULL DEFAULT 'all',
  rank         INTEGER NOT NULL,
  video_id     TEXT NOT NULL,
  PRIMARY KEY (collected_at, region, category_id, rank)
);

CREATE TABLE IF NOT EXISTS channel_pool (
  channel_id      TEXT PRIMARY KEY,
  channel_title   TEXT,
  first_seen_at   TEXT NOT NULL,
  source          TEXT,
  screened_at     TEXT,
  passed_filter   INTEGER,
  group_type      TEXT,
  decision        TEXT,
  decision_reason TEXT,
  tier            TEXT DEFAULT 'panel'
);

CREATE TABLE IF NOT EXISTS channel_stats (
  channel_id       TEXT NOT NULL,
  collected_at     TEXT NOT NULL,
  run_id           TEXT NOT NULL,
  subscriber_count INTEGER,
  view_count       INTEGER,
  video_count      INTEGER,
  PRIMARY KEY (channel_id, collected_at)
);

CREATE TABLE IF NOT EXISTS channel_collection_state (
  channel_id                 TEXT PRIMARY KEY,
  backfill_status             TEXT,
  backfill_completed_at       TEXT,
  oldest_video_published_at   TEXT,
  last_incremental_at         TEXT
);

CREATE VIEW IF NOT EXISTS channel_trending_days AS
SELECT v.channel_id,
       COUNT(DISTINCT substr(t.collected_at, 1, 10)) AS days_in_trending,
       COUNT(DISTINCT t.video_id)                    AS videos_in_trending,
       MIN(t.collected_at)                           AS first_trending_at
FROM trending_rank t
JOIN videos v ON v.video_id = t.video_id
GROUP BY v.channel_id;
"""


def get_connection(db_path: str) -> sqlite3.Connection:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


# --- runs -------------------------------------------------------------

def start_run(conn: sqlite3.Connection, run_id: str, job_name: str, started_at: str) -> None:
    conn.execute(
        "INSERT INTO runs (run_id, job_name, started_at, status) VALUES (?, ?, ?, 'running')",
        (run_id, job_name, started_at),
    )
    conn.commit()


def finish_run(
    conn: sqlite3.Connection,
    run_id: str,
    finished_at: str,
    status: str,
    quota_used: int,
    items_seen: int,
    error_message: str = None,
) -> None:
    conn.execute(
        """UPDATE runs SET finished_at=?, status=?, quota_used=?, items_seen=?, error_message=?
           WHERE run_id=?""",
        (finished_at, status, quota_used, items_seen, error_message, run_id),
    )
    conn.commit()


# --- videos / stats -----------------------------------------------------

def upsert_video(conn: sqlite3.Connection, video: dict) -> None:
    conn.execute(
        """INSERT INTO videos
             (video_id, channel_id, title, published_at, duration_sec, category_id,
              tags, thumbnail_url, first_seen_at, last_meta_at)
           VALUES
             (:video_id, :channel_id, :title, :published_at, :duration_sec, :category_id,
              :tags, :thumbnail_url, :first_seen_at, :last_meta_at)
           ON CONFLICT(video_id) DO UPDATE SET
             channel_id=excluded.channel_id,
             title=excluded.title,
             published_at=excluded.published_at,
             duration_sec=excluded.duration_sec,
             category_id=excluded.category_id,
             tags=excluded.tags,
             thumbnail_url=excluded.thumbnail_url,
             last_meta_at=excluded.last_meta_at
        """,
        video,
    )


def record_meta_history_if_changed(conn: sqlite3.Connection, video_id: str, observed_at: str,
                                    title: str, thumbnail_url: str) -> None:
    row = conn.execute(
        """SELECT title, thumbnail_url FROM video_meta_history
           WHERE video_id=? ORDER BY observed_at DESC LIMIT 1""",
        (video_id,),
    ).fetchone()
    if row is not None and row["title"] == title and row["thumbnail_url"] == thumbnail_url:
        return
    conn.execute(
        "INSERT OR IGNORE INTO video_meta_history (video_id, observed_at, title, thumbnail_url) VALUES (?, ?, ?, ?)",
        (video_id, observed_at, title, thumbnail_url),
    )


def insert_video_stats(conn: sqlite3.Connection, stats: dict) -> None:
    conn.execute(
        """INSERT OR IGNORE INTO video_stats
             (video_id, collected_at, run_id, view_count, like_count, comment_count)
           VALUES (:video_id, :collected_at, :run_id, :view_count, :like_count, :comment_count)""",
        stats,
    )


def insert_trending_rank(conn: sqlite3.Connection, row: dict) -> None:
    conn.execute(
        """INSERT OR IGNORE INTO trending_rank
             (collected_at, run_id, region, category_id, rank, video_id)
           VALUES (:collected_at, :run_id, :region, :category_id, :rank, :video_id)""",
        row,
    )


# --- channels -------------------------------------------------------------

def upsert_channel_pool(conn: sqlite3.Connection, channel: dict) -> None:
    """Insert a new channel candidate. Existing rows (and their manual decision/tier) are left untouched."""
    channel.setdefault("tier", "panel")
    conn.execute(
        """INSERT OR IGNORE INTO channel_pool
             (channel_id, channel_title, first_seen_at, source, screened_at,
              passed_filter, group_type, decision, decision_reason, tier)
           VALUES
             (:channel_id, :channel_title, :first_seen_at, :source, :screened_at,
              :passed_filter, :group_type, :decision, :decision_reason, :tier)""",
        channel,
    )


def update_channel_decision(conn: sqlite3.Connection, channel_id: str, decision: str,
                             decision_reason: str = None, group_type: str = None) -> None:
    conn.execute(
        """UPDATE channel_pool SET decision=?, decision_reason=?,
             group_type=COALESCE(?, group_type)
           WHERE channel_id=?""",
        (decision, decision_reason, group_type, channel_id),
    )


def get_included_channels(conn: sqlite3.Connection, tier: str = None) -> list:
    if tier:
        rows = conn.execute(
            "SELECT * FROM channel_pool WHERE decision='include' AND tier=?", (tier,)
        ).fetchall()
    else:
        rows = conn.execute("SELECT * FROM channel_pool WHERE decision='include'").fetchall()
    return [dict(r) for r in rows]


def insert_channel_stats(conn: sqlite3.Connection, stats: dict) -> None:
    conn.execute(
        """INSERT OR IGNORE INTO channel_stats
             (channel_id, collected_at, run_id, subscriber_count, view_count, video_count)
           VALUES (:channel_id, :collected_at, :run_id, :subscriber_count, :view_count, :video_count)""",
        stats,
    )


def get_backfill_queue(conn: sqlite3.Connection) -> list:
    """Channels pending backfill, core tier first, then panel FIFO by first_seen_at."""
    rows = conn.execute(
        """SELECT cp.* FROM channel_pool cp
           LEFT JOIN channel_collection_state ccs ON ccs.channel_id = cp.channel_id
           WHERE cp.decision = 'include'
             AND (ccs.backfill_status IS NULL OR ccs.backfill_status IN ('pending', 'failed'))
           ORDER BY CASE cp.tier WHEN 'core' THEN 0 ELSE 1 END, cp.first_seen_at ASC"""
    ).fetchall()
    return [dict(r) for r in rows]


def upsert_channel_collection_state(conn: sqlite3.Connection, channel_id: str, **fields) -> None:
    existing = conn.execute(
        "SELECT channel_id FROM channel_collection_state WHERE channel_id=?", (channel_id,)
    ).fetchone()
    if existing is None:
        conn.execute(
            "INSERT INTO channel_collection_state (channel_id) VALUES (?)", (channel_id,)
        )
    if fields:
        set_clause = ", ".join(f"{k}=?" for k in fields)
        conn.execute(
            f"UPDATE channel_collection_state SET {set_clause} WHERE channel_id=?",
            (*fields.values(), channel_id),
        )


def get_growth_window_video_ids(conn: sqlite3.Connection, channel_ids: list, window_days: int) -> list:
    if not channel_ids:
        return []
    placeholders = ",".join("?" for _ in channel_ids)
    rows = conn.execute(
        f"""SELECT video_id FROM videos
            WHERE channel_id IN ({placeholders})
              AND published_at >= datetime('now', ?)""",
        (*channel_ids, f"-{window_days} days"),
    ).fetchall()
    return [r["video_id"] for r in rows]
