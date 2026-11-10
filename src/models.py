"""Data model for an influencer record as it moves through the pipeline."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields
from typing import Any

NOT_FOUND = "Not Found"
NOT_AVAILABLE = "Not Available"

# Pipeline stages, in order
STAGE_DISCOVERED = "discovered"
STAGE_COLLECTED = "collected"
STAGE_FILTERED = "filtered"
STAGE_ENRICHED = "enriched"

AUDIENCE_UNAVAILABLE = (
    "Not Available (YouTube only exposes audience demographics to the channel owner via YouTube Analytics)"
)


@dataclass
class Influencer:
    channel_id: str
    name: str
    platform: str = "YouTube"
    handle: str | None = None
    profile_url: str = ""
    country: str | None = None
    default_language: str | None = None

    # Raw channel stats (from the YouTube Data API)
    subscribers: int | None = None
    subscribers_hidden: bool = False
    total_views: int | None = None
    video_count: int | None = None
    channel_created_at: str | None = None
    description: str = ""
    channel_keywords: str = ""
    topic_categories: list[str] = field(default_factory=list)
    made_for_kids: bool | None = None
    recent_videos: list[dict[str, Any]] = field(default_factory=list)

    # Computed metrics
    engagement_rate: float | None = None
    avg_views: float | None = None
    median_views: float | None = None
    views_to_subs: float | None = None
    shorts_share: float | None = None
    videos_analysed: int = 0
    last_upload_at: str | None = None
    days_since_upload: int | None = None

    # Contact details (only ever extracted from public text, never guessed)
    email: str = NOT_FOUND
    email_source: str | None = None
    other_emails: list[str] = field(default_factory=list)
    instagram_url: str | None = None
    tiktok_url: str | None = None
    twitter_url: str | None = None
    linkedin_url: str | None = None
    website: str | None = None

    # Classification / filtering
    category: str | None = None
    sub_niche: str | None = None
    relevance_score: float | None = None
    educational_ratio: float | None = None
    tier: str | None = None
    filter_status: str = "PENDING"          # PENDING | PASS | FAIL
    filter_checks: list[dict[str, Any]] = field(default_factory=list)
    fail_reasons: list[str] = field(default_factory=list)

    # Enrichment
    content_themes: list[str] = field(default_factory=list)
    themes_source: str | None = None        # llm | keyword-fallback
    content_tone: str | None = None
    inferred_audience: str | None = None    # inferred from content, NOT analytics
    standout_video: str | None = None
    audience_age: str = AUDIENCE_UNAVAILABLE
    audience_gender: str = AUDIENCE_UNAVAILABLE
    audience_geography: str = AUDIENCE_UNAVAILABLE

    # Bookkeeping
    discovered_via: list[str] = field(default_factory=list)
    discovered_at: str | None = None
    stage: str = STAGE_DISCOVERED
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Influencer":
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in known})

    @property
    def has_valid_email(self) -> bool:
        from .extractors import is_valid_email

        return bool(self.email) and self.email != NOT_FOUND and is_valid_email(self.email)
