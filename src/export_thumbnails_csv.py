"""One-off export: videos that have a mirrored thumbnail, as a CSV.

Usage:
    python -m src.export_thumbnails_csv [output_path]
"""
import csv
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

from src import db
from src.storage_client import SupabaseStorageClient

ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    load_dotenv(ROOT / ".env")
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        print("DATABASE_URL is not set", file=sys.stderr)
        return 1

    output_path = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "data" / "exports" / "thumbnails.csv"
    output_path.parent.mkdir(parents=True, exist_ok=True)

    storage = SupabaseStorageClient(
        os.environ.get("SUPABASE_URL", ""), os.environ.get("SUPABASE_SERVICE_KEY", "")
    )

    conn = db.get_connection(database_url)
    rows = conn.execute("""
        SELECT v.video_id, v.channel_id, cp.channel_title, v.title, v.published_at,
               v.thumbnail_url AS original_thumbnail_url, v.thumbnail_path
        FROM videos v
        LEFT JOIN channel_pool cp ON cp.channel_id = v.channel_id
        WHERE v.thumbnail_path IS NOT NULL
        ORDER BY v.first_seen_at DESC
    """).fetchall()
    conn.close()

    with open(output_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow([
            "video_id", "channel_id", "channel_title", "title", "published_at",
            "original_thumbnail_url", "thumbnail_path", "stored_public_url",
        ])
        for r in rows:
            writer.writerow([
                r["video_id"], r["channel_id"], r["channel_title"], r["title"], r["published_at"],
                r["original_thumbnail_url"], r["thumbnail_path"], storage.public_url(r["thumbnail_path"]),
            ])

    print(f"wrote {len(rows)} rows to {output_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
