"""YouTube Data API v3 client: auth, retry/backoff, batching, raw-response archiving.

Retry policy (docs/trending-collector-spec.md #5):
- 5xx / 429: exponential backoff, up to `max_retries` attempts
- 403 quotaExceeded: raise immediately, no retry (caller must stop the whole job)
- other 4xx: raise immediately, no retry (caller logs and skips)
"""
import gzip
import json
import logging
import time
from pathlib import Path

import requests

log = logging.getLogger(__name__)

API_BASE = "https://www.googleapis.com/youtube/v3"
BATCH_SIZE = 50


class QuotaExceededError(Exception):
    pass


class YouTubeAPIError(Exception):
    pass


class YouTubeClient:
    def __init__(self, api_key: str, session: requests.Session = None,
                 max_retries: int = 3, backoff_base: float = 2.0):
        if not api_key:
            raise ValueError("api_key is required")
        self.api_key = api_key
        self.session = session or requests.Session()
        self.max_retries = max_retries
        self.backoff_base = backoff_base
        self.quota_used = 0

    def _get(self, endpoint: str, params: dict, unit_cost: int = 1) -> dict:
        request_params = {**params, "key": self.api_key}
        url = f"{API_BASE}/{endpoint}"
        last_error = None
        for attempt in range(self.max_retries + 1):
            resp = self.session.get(url, params=request_params, timeout=30)
            if resp.status_code == 200:
                self.quota_used += unit_cost
                return resp.json()
            if resp.status_code == 403 and "quotaExceeded" in resp.text:
                raise QuotaExceededError(resp.text)
            if resp.status_code >= 500 or resp.status_code == 429:
                wait = self.backoff_base ** attempt
                log.warning(
                    "retryable error %s on %s (attempt %d/%d), sleeping %.1fs",
                    resp.status_code, endpoint, attempt + 1, self.max_retries + 1, wait,
                )
                last_error = YouTubeAPIError(f"{resp.status_code}: {resp.text}")
                time.sleep(wait)
                continue
            raise YouTubeAPIError(f"{resp.status_code}: {resp.text}")
        raise last_error

    def videos_list(self, part: str, chart: str = None, region_code: str = None,
                     video_category_id: str = None, ids: list = None,
                     max_results: int = 50, page_token: str = None, unit_cost: int = 1) -> dict:
        params = {"part": part, "maxResults": max_results}
        if chart:
            params["chart"] = chart
        if region_code:
            params["regionCode"] = region_code
        if video_category_id:
            params["videoCategoryId"] = video_category_id
        if ids:
            params["id"] = ",".join(ids)
        if page_token:
            params["pageToken"] = page_token
        return self._get("videos", params, unit_cost=unit_cost)

    def video_categories_list(self, region_code: str, unit_cost: int = 1) -> dict:
        return self._get("videoCategories", {"part": "snippet", "regionCode": region_code}, unit_cost=unit_cost)

    def channels_list(self, ids: list, part: str = "snippet,contentDetails,statistics", unit_cost: int = 1) -> dict:
        return self._get("channels", {"part": part, "id": ",".join(ids)}, unit_cost=unit_cost)

    def playlist_items_list(self, playlist_id: str, max_results: int = 50,
                             page_token: str = None, unit_cost: int = 1) -> dict:
        params = {"part": "contentDetails", "playlistId": playlist_id, "maxResults": max_results}
        if page_token:
            params["pageToken"] = page_token
        return self._get("playlistItems", params, unit_cost=unit_cost)

    def search_list(self, q: str, region_code: str = None, order: str = "relevance",
                     video_type: str = "video", max_results: int = 50,
                     page_token: str = None, unit_cost: int = 100) -> dict:
        params = {"part": "snippet", "q": q, "type": video_type, "order": order, "maxResults": max_results}
        if region_code:
            params["regionCode"] = region_code
        if page_token:
            params["pageToken"] = page_token
        return self._get("search", params, unit_cost=unit_cost)


def chunked(items, size: int = BATCH_SIZE):
    items = list(items)
    for i in range(0, len(items), size):
        yield items[i:i + size]


def save_raw_response(data: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
