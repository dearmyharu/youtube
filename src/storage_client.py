"""Minimal Supabase Storage REST client (no supabase-py dependency, same style
as youtube_client.py — plain `requests` calls).

Used to mirror video thumbnails into a persistent bucket, since GitHub Actions
runners are ephemeral and can't hold onto downloaded images between runs.
"""
import logging

import requests

log = logging.getLogger(__name__)


class StorageError(Exception):
    pass


class SupabaseStorageClient:
    def __init__(self, project_url: str, service_key: str, bucket: str = "thumbnails",
                 session: requests.Session = None):
        self.base = project_url.rstrip("/") + "/storage/v1"
        self.bucket = bucket
        self.service_key = service_key
        self.session = session or requests.Session()

    def _headers(self, extra: dict = None) -> dict:
        headers = {"Authorization": f"Bearer {self.service_key}", "apikey": self.service_key}
        if extra:
            headers.update(extra)
        return headers

    def ensure_bucket(self, public: bool = True) -> None:
        resp = self.session.post(
            f"{self.base}/bucket", headers=self._headers(),
            json={"name": self.bucket, "public": public}, timeout=30,
        )
        if resp.status_code in (200, 201):
            return
        if "already exists" in resp.text.lower() or resp.status_code == 409:
            return
        raise StorageError(f"bucket create failed: {resp.status_code} {resp.text}")

    def upload(self, path: str, content: bytes, content_type: str = "image/jpeg") -> str:
        headers = self._headers({"Content-Type": content_type, "x-upsert": "true"})
        resp = self.session.post(
            f"{self.base}/object/{self.bucket}/{path}", headers=headers, data=content, timeout=30,
        )
        if resp.status_code not in (200, 201):
            raise StorageError(f"upload failed for {path}: {resp.status_code} {resp.text}")
        return path

    def public_url(self, path: str) -> str:
        return f"{self.base}/object/public/{self.bucket}/{path}"


def save_thumbnail(storage: SupabaseStorageClient, video_id: str, thumbnail_url: str,
                    session: requests.Session = None) -> str:
    """Download a video's thumbnail and upload it to Storage. Returns the stored path.
    Raises StorageError/requests exceptions on failure — callers should catch and log,
    a failed thumbnail must never abort the whole collection run."""
    sess = session or requests.Session()
    resp = sess.get(thumbnail_url, timeout=30)
    resp.raise_for_status()
    content_type = resp.headers.get("Content-Type", "image/jpeg")
    ext = "png" if "png" in content_type else "jpg"
    path = f"{video_id}.{ext}"
    storage.upload(path, resp.content, content_type=content_type)
    return path
