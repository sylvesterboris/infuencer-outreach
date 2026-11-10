"""Engagement and activity metrics computed from real per-video statistics."""
from __future__ import annotations

import statistics
from datetime import datetime
from typing import Any

from .utils import parse_iso, parse_iso8601_duration, safe_int, utc_now

SHORTS_MAX_SECONDS = 180  # YouTube Shorts can be up to 3 minutes since Oct 2024


def video_from_api(item: dict[str, Any]) -> dict[str, Any]:
    """Flatten a videos.list item into the small dict we store."""
    sn = item.get("snippet", {})
    st = item.get("statistics", {})
    cd = item.get("contentDetails", {})
    return {
        "video_id": item.get("id"),
        "title": sn.get("title", ""),
        "published_at": sn.get("publishedAt"),
        "views": safe_int(st.get("viewCount")),
        # likeCount/commentCount are absent when the creator hides likes / disables comments
        "likes": safe_int(st.get("likeCount")),
        "comments": safe_int(st.get("commentCount")),
        "duration_seconds": parse_iso8601_duration(cd.get("duration")),
        "tags": (sn.get("tags") or [])[:15],
        "description": (sn.get("description") or "")[:1500],
        "live": sn.get("liveBroadcastContent", "none") != "none",
    }


def compute_metrics(
    videos: list[dict[str, Any]],
    subscribers: int | None,
    videos_for_engagement: int = 10,
    min_video_age_days: int = 3,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Engagement rate = sum(likes + comments) / sum(views) over recent, settled videos.

    Videos with hidden likes are excluded from the ER calculation (we never substitute a
    guess). If no video has usable stats, engagement_rate is None (reported as Not Available).
    """
    now = now or utc_now()
    ordered = sorted(
        (v for v in videos if parse_iso(v.get("published_at"))),
        key=lambda v: parse_iso(v["published_at"]),
        reverse=True,
    )

    last_upload = parse_iso(ordered[0]["published_at"]) if ordered else None
    days_since = (now - last_upload).days if last_upload else None

    settled = [
        v
        for v in ordered
        if not v.get("live") and (now - parse_iso(v["published_at"])).days >= min_video_age_days
    ][:videos_for_engagement]

    usable = [v for v in settled if v.get("views") and v.get("likes") is not None]
    total_views = sum(v["views"] for v in usable)
    total_interactions = sum(v["likes"] + (v.get("comments") or 0) for v in usable)
    er = round(total_interactions / total_views, 5) if total_views > 0 else None

    views = [v["views"] for v in settled if v.get("views") is not None]
    avg_views = round(sum(views) / len(views), 1) if views else None
    median_views = float(statistics.median(views)) if views else None

    durations = [v["duration_seconds"] for v in ordered if v.get("duration_seconds")]
    shorts_share = (
        round(sum(1 for d in durations if d <= SHORTS_MAX_SECONDS) / len(durations), 3) if durations else None
    )

    return {
        "engagement_rate": er,
        "videos_analysed": len(usable),
        "avg_views": avg_views,
        "median_views": median_views,
        "views_to_subs": round(median_views / subscribers, 4) if median_views and subscribers else None,
        "shorts_share": shorts_share,
        "last_upload_at": last_upload.isoformat() if last_upload else None,
        "days_since_upload": days_since,
    }


def tier_for(subscribers: int | None) -> str:
    if subscribers is None:
        return "Unknown"
    if subscribers < 5_000:
        return "Nano (<5K)"
    if subscribers < 20_000:
        return "Micro - emerging (5K-20K)"
    if subscribers < 50_000:
        return "Micro - mid (20K-50K)"
    if subscribers <= 100_000:
        return "Micro - upper (50K-100K)"
    if subscribers <= 1_000_000:
        return "Macro (100K-1M)"
    return "Mega (1M+)"
