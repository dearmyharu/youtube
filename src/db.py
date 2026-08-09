"""Postgres (Supabase) schema and access helpers, shared by the trending and channel collectors.

Design principles (see docs/trending-collector-spec.md #0):
- observation tables (video_stats, trending_rank, channel_stats) are append-only
- `videos` is upserted but `first_seen_at` is never overwritten
- `channel_collection_state` is the only table that is freely UPDATEd (bookkeeping, not an observation)
"""
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import psycopg2
import psycopg2.extras

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
  view_count    BIGINT,
  like_count    BIGINT,
  comment_count BIGINT,
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
  subscriber_count BIGINT,
  view_count       BIGINT,
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

CREATE OR REPLACE VIEW channel_trending_days AS
SELECT v.channel_id,
       COUNT(DISTINCT substr(t.collected_at, 1, 10)) AS days_in_trending,
       COUNT(DISTINCT t.video_id)                    AS videos_in_trending,
       MIN(t.collected_at)                           AS first_trending_at
FROM trending_rank t
JOIN videos v ON v.video_id = t.video_id
GROUP BY v.channel_id;
"""


class Connection:
    """Thin wrapper so call sites can use sqlite3.Connection-style .execute()/.commit()."""

    def __init__(self, pg_conn):
        self._conn = pg_conn

    def execute(self, sql, params=None):
        cur = self._conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(sql, params)
        return cur

    def executescript(self, sql):
        cur = self._conn.cursor()
        cur.execute(sql)

    def commit(self):
        self._conn.commit()

    def close(self):
        self._conn.close()


def _strip_unsupported_query_params(database_url: str) -> str:
    """Drop query params libpq/psycopg2 rejects (e.g. Supabase's `pgbouncer=true` hint,
    which is meant for ORMs like Prisma, not for a raw psycopg2 DSN)."""
    parsed = urlparse(database_url)
    if not parsed.query:
        return database_url
    kept = [(k, v) for k, v in parse_qsl(parsed.query) if k != "pgbouncer"]
    return urlunparse(parsed._replace(query=urlencode(kept)))


def get_connection(database_url: str) -> Connection:
    return Connection(psycopg2.connect(_strip_unsupported_query_params(database_url)))


def init_db(conn: Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


# --- runs -------------------------------------------------------------

def start_run(conn: Connection, run_id: str, job_name: str, started_at: str) -> None:
    conn.execute(
        "INSERT INTO runs (run_id, job_name, started_at, status) VALUES (%s, %s, %s, 'running')",
        (run_id, job_name, started_at),
    )
    conn.commit()


def finish_run(
    conn: Connection,
    run_id: str,
    finished_at: str,
    status: str,
    quota_used: int,
    items_seen: int,
    error_message: str = None,
) -> None:
    conn.execute(
        """UPDATE runs SET finished_at=%s, status=%s, quota_used=%s, items_seen=%s, error_message=%s
           WHERE run_id=%s""",
        (finished_at, status, quota_used, items_seen, error_message, run_id),
    )
    conn.commit()


def get_quota_used_today(conn: Connection, day: str) -> int:
    """Sum of quota_used across all jobs (trending + channel backfill/incremental) whose
    started_at falls on `day` (YYYY-MM-DD, UTC). Lets multiple same-day runs (e.g. several
    backfill batches spread across the day) see what earlier runs already spent."""
    row = conn.execute(
        "SELECT COALESCE(SUM(quota_used), 0) AS used FROM runs WHERE started_at::date = %s",
        (day,),
    ).fetchone()
    return row["used"] or 0


# --- videos / stats -----------------------------------------------------

def upsert_video(conn: Connection, video: dict) -> None:
    conn.execute(
        """INSERT INTO videos
             (video_id, channel_id, title, published_at, duration_sec, category_id,
              tags, thumbnail_url, first_seen_at, last_meta_at)
           VALUES
             (%(video_id)s, %(channel_id)s, %(title)s, %(published_at)s, %(duration_sec)s, %(category_id)s,
              %(tags)s, %(thumbnail_url)s, %(first_seen_at)s, %(last_meta_at)s)
           ON CONFLICT (video_id) DO UPDATE SET
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


def record_meta_history_if_changed(conn: Connection, video_id: str, observed_at: str,
                                    title: str, thumbnail_url: str) -> None:
    row = conn.execute(
        """SELECT title, thumbnail_url FROM video_meta_history
           WHERE video_id=%s ORDER BY observed_at DESC LIMIT 1""",
        (video_id,),
    ).fetchone()
    if row is not None and row["title"] == title and row["thumbnail_url"] == thumbnail_url:
        return
    conn.execute(
        """INSERT INTO video_meta_history (video_id, observed_at, title, thumbnail_url)
           VALUES (%s, %s, %s, %s)
           ON CONFLICT (video_id, observed_at) DO NOTHING""",
        (video_id, observed_at, title, thumbnail_url),
    )


def insert_video_stats(conn: Connection, stats: dict) -> None:
    conn.execute(
        """INSERT INTO video_stats
             (video_id, collected_at, run_id, view_count, like_count, comment_count)
           VALUES (%(video_id)s, %(collected_at)s, %(run_id)s, %(view_count)s, %(like_count)s, %(comment_count)s)
           ON CONFLICT (video_id, collected_at) DO NOTHING""",
        stats,
    )


def insert_trending_rank(conn: Connection, row: dict) -> None:
    conn.execute(
        """INSERT INTO trending_rank
             (collected_at, run_id, region, category_id, rank, video_id)
           VALUES (%(collected_at)s, %(run_id)s, %(region)s, %(category_id)s, %(rank)s, %(video_id)s)
           ON CONFLICT (collected_at, region, category_id, rank) DO NOTHING""",
        row,
    )


# --- channels -------------------------------------------------------------

def upsert_channel_pool(conn: Connection, channel: dict) -> None:
    """Insert a new channel candidate. Existing rows (and their manual decision/tier) are left untouched."""
    channel.setdefault("tier", "panel")
    conn.execute(
        """INSERT INTO channel_pool
             (channel_id, channel_title, first_seen_at, source, screened_at,
              passed_filter, group_type, decision, decision_reason, tier)
           VALUES
             (%(channel_id)s, %(channel_title)s, %(first_seen_at)s, %(source)s, %(screened_at)s,
              %(passed_filter)s, %(group_type)s, %(decision)s, %(decision_reason)s, %(tier)s)
           ON CONFLICT (channel_id) DO NOTHING""",
        channel,
    )


def update_channel_decision(conn: Connection, channel_id: str, decision: str,
                             decision_reason: str = None, group_type: str = None) -> None:
    conn.execute(
        """UPDATE channel_pool SET decision=%s, decision_reason=%s,
             group_type=COALESCE(%s, group_type)
           WHERE channel_id=%s""",
        (decision, decision_reason, group_type, channel_id),
    )


def get_included_channels(conn: Connection, tier: str = None) -> list:
    if tier:
        rows = conn.execute(
            "SELECT * FROM channel_pool WHERE decision='include' AND tier=%s", (tier,)
        ).fetchall()
    else:
        rows = conn.execute("SELECT * FROM channel_pool WHERE decision='include'").fetchall()
    return [dict(r) for r in rows]


def insert_channel_stats(conn: Connection, stats: dict) -> None:
    conn.execute(
        """INSERT INTO channel_stats
             (channel_id, collected_at, run_id, subscriber_count, view_count, video_count)
           VALUES (%(channel_id)s, %(collected_at)s, %(run_id)s, %(subscriber_count)s, %(view_count)s, %(video_count)s)
           ON CONFLICT (channel_id, collected_at) DO NOTHING""",
        stats,
    )


def get_backfill_queue(conn: Connection) -> list:
    """Channels pending backfill, core tier first, then panel FIFO by first_seen_at."""
    rows = conn.execute(
        """SELECT cp.* FROM channel_pool cp
           LEFT JOIN channel_collection_state ccs ON ccs.channel_id = cp.channel_id
           WHERE cp.decision = 'include'
             AND (ccs.backfill_status IS NULL OR ccs.backfill_status IN ('pending', 'failed'))
           ORDER BY CASE cp.tier WHEN 'core' THEN 0 ELSE 1 END, cp.first_seen_at ASC"""
    ).fetchall()
    return [dict(r) for r in rows]


def upsert_channel_collection_state(conn: Connection, channel_id: str, **fields) -> None:
    existing = conn.execute(
        "SELECT channel_id FROM channel_collection_state WHERE channel_id=%s", (channel_id,)
    ).fetchone()
    if existing is None:
        conn.execute(
            "INSERT INTO channel_collection_state (channel_id) VALUES (%s)", (channel_id,)
        )
    if fields:
        set_clause = ", ".join(f"{k}=%s" for k in fields)
        conn.execute(
            f"UPDATE channel_collection_state SET {set_clause} WHERE channel_id=%s",
            (*fields.values(), channel_id),
        )


def get_growth_window_video_ids(conn: Connection, channel_ids: list, window_days: int) -> list:
    if not channel_ids:
        return []
    placeholders = ",".join("%s" for _ in channel_ids)
    rows = conn.execute(
        f"""SELECT video_id FROM videos
            WHERE channel_id IN ({placeholders})
              AND published_at::timestamptz >= NOW() - make_interval(days => %s)""",
        (*channel_ids, window_days),
    ).fetchall()
    return [r["video_id"] for r in rows]
