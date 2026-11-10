"""Thin YouTube Data API v3 client with quota accounting, caching and retries.

Quota costs (units) per the YouTube Data API docs: search.list = 100, channels.list,
playlistItems.list and videos.list = 1 each. The default project quota is 10,000/day.
"""
from __future__ import annotations

import hashlib
import json
import time
from typing import Any

import requests

from .storage import Storage
from .utils import get_logger

BASE_URL = "https://www.googleapis.com/youtube/v3"
QUOTA_COST = {"search": 100, "channels": 1, "playlistItems": 1, "videos": 1}


class YouTubeError(Exception):
    pass


class QuotaExceeded(YouTubeError):
    """YouTube refused the request because the daily project quota is used up."""


class QuotaBudgetExceeded(YouTubeError):
    """Our own safety budget (config youtube.daily_quota_budget) would be exceeded."""


class YouTubeClient:
    def __init__(
        self,
        api_key: str,
        storage: Storage | None = None,
        quota_budget: int = 8000,
        cache_ttl_hours: float = 24,
        timeout: float = 20,
        session: requests.Session | None = None,
        max_retries: int = 3,
    ):
        self.api_key = api_key
        self.storage = storage
        self.quota_budget = quota_budget
        self.cache_ttl_hours = cache_ttl_hours
        self.timeout = timeout
        self.session = session or requests.Session()
        self.max_retries = max_retries
        self.units_used = 0
        self.log = get_logger()

    # ------------------------------------------------------------------ core
    def _get(self, endpoint: str, params: dict[str, Any]) -> dict[str, Any]:
        clean = {k: v for k, v in params.items() if v not in (None, "")}
        cache_key = endpoint + ":" + hashlib.sha256(json.dumps(clean, sort_keys=True).encode()).hexdigest()
        if self.storage is not None:
            cached = self.storage.cache_get(cache_key, self.cache_ttl_hours)
            if cached is not None:
                return cached

        cost = QUOTA_COST.get(endpoint, 1)
        if self.units_used + cost > self.quota_budget:
            raise QuotaBudgetExceeded(
                f"Quota budget of {self.quota_budget} units reached ({self.units_used} used this run)"
            )

        url = f"{BASE_URL}/{endpoint}"
        last_error: str = ""
        for attempt in range(1, self.max_retries + 1):
            try:
                resp = self.session.get(url, params={**clean, "key": self.api_key}, timeout=self.timeout)
            except requests.RequestException as exc:
                last_error = f"network error: {exc}"
                self._backoff(attempt)
                continue

            if resp.status_code == 200:
                self.units_used += cost
                data = resp.json()
                if self.storage is not None:
                    self.storage.cache_set(cache_key, data)
                return data

            reason = self._error_reason(resp)
            if resp.status_code == 403 and reason in {"quotaExceeded", "dailyLimitExceeded", "rateLimitExceeded"}:
                if reason == "rateLimitExceeded":
                    last_error = reason
                    self._backoff(attempt)
                    continue
                raise QuotaExceeded(f"YouTube API quota exhausted ({reason})")
            if resp.status_code in (429, 500, 502, 503, 504):
                last_error = f"HTTP {resp.status_code}"
                self._backoff(attempt)
                continue
            raise YouTubeError(f"{endpoint} failed: HTTP {resp.status_code} {reason or resp.text[:200]}")

        raise YouTubeError(f"{endpoint} failed after {self.max_retries} attempts: {last_error}")

    @staticmethod
    def _error_reason(resp: requests.Response) -> str:
        try:
            errors = resp.json().get("error", {}).get("errors", [])
            return errors[0].get("reason", "") if errors else ""
        except ValueError:
            return ""

    def _backoff(self, attempt: int) -> None:
        delay = min(2 ** attempt, 20)
        self.log.debug("Retrying YouTube request in %ss", delay)
        time.sleep(delay)

    # ------------------------------------------------------------------ endpoints
    def search(
        self,
        query: str,
        search_type: str = "video",
        max_results: int = 50,
        page_token: str | None = None,
        published_after: str | None = None,
        region_code: str | None = None,
        relevance_language: str | None = None,
    ) -> tuple[list[dict[str, Any]], str | None]:
        params = {
            "part": "snippet",
            "q": query,
            "type": search_type,
            "maxResults": min(max_results, 50),
            "pageToken": page_token,
            "regionCode": region_code,
            "relevanceLanguage": relevance_language,
            "order": "relevance",
        }
        if search_type == "video" and published_after:
            params["publishedAfter"] = published_after
        data = self._get("search", params)
        return data.get("items", []), data.get("nextPageToken")

    def get_channels(self, channel_ids: list[str]) -> dict[str, dict[str, Any]]:
        """channels.list in batches of 50. Returns {channel_id: resource}."""
        out: dict[str, dict[str, Any]] = {}
        for i in range(0, len(channel_ids), 50):
            batch = channel_ids[i : i + 50]
            data = self._get(
                "channels",
                {
                    "part": "snippet,statistics,brandingSettings,topicDetails,contentDetails,status",
                    "id": ",".join(batch),
                    "maxResults": 50,
                },
            )
            for item in data.get("items", []):
                out[item["id"]] = item
        return out

    def get_upload_video_ids(self, uploads_playlist_id: str, limit: int = 12) -> list[str]:
        data = self._get(
            "playlistItems",
            {"part": "contentDetails", "playlistId": uploads_playlist_id, "maxResults": min(limit, 50)},
        )
        return [it["contentDetails"]["videoId"] for it in data.get("items", []) if it.get("contentDetails")]

    def get_videos(self, video_ids: list[str]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for i in range(0, len(video_ids), 50):
            batch = video_ids[i : i + 50]
            data = self._get(
                "videos", {"part": "snippet,statistics,contentDetails", "id": ",".join(batch), "maxResults": 50}
            )
            out.extend(data.get("items", []))
        return out
