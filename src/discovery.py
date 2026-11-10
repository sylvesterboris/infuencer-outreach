"""Stage 1-2: discover candidate channels and collect their real stats.

discover(): runs keyword searches and records unique channel IDs.
collect():  pulls channel stats + recent uploads, computes metrics and extracts
            public contact details from channel/video descriptions.
"""
from __future__ import annotations

from datetime import timedelta
from typing import Any

from .extractors import extract_emails, extract_social_links
from .metrics import compute_metrics, tier_for, video_from_api
from .models import STAGE_COLLECTED, STAGE_DISCOVERED, Influencer, NOT_FOUND
from .storage import Storage
from .utils import get_logger, now_iso, safe_int, utc_now
from .youtube_client import QuotaBudgetExceeded, QuotaExceeded, YouTubeClient, YouTubeError


def discover(cfg: dict[str, Any], yt: YouTubeClient, storage: Storage) -> dict[str, Any]:
    log = get_logger()
    dcfg = cfg["discovery"]
    target = int(dcfg.get("target_candidates", 150))
    existing = storage.influencer_ids()
    published_after = (utc_now() - timedelta(days=int(dcfg.get("published_within_days", 90)))).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )

    found: dict[str, Influencer] = {}
    queries_run = 0
    stopped_reason = None

    for query in dcfg.get("queries", []):
        if len(existing) + len(found) >= target:
            break
        token = None
        for _page in range(int(dcfg.get("pages_per_query", 1))):
            try:
                items, token = yt.search(
                    query=query,
                    search_type=dcfg.get("search_type", "video"),
                    max_results=int(dcfg.get("results_per_page", 50)),
                    page_token=token,
                    published_after=published_after,
                    region_code=dcfg.get("region_code"),
                    relevance_language=dcfg.get("relevance_language"),
                )
            except (QuotaBudgetExceeded, QuotaExceeded) as exc:
                stopped_reason = str(exc)
                break
            except YouTubeError as exc:
                log.warning("Search '%s' failed: %s", query, exc)
                break
            queries_run += 1

            for item in items:
                sn = item.get("snippet", {})
                cid = sn.get("channelId") or item.get("id", {}).get("channelId")
                if not cid:
                    continue
                if cid in existing:
                    rec = storage.get_influencer(cid)
                    if rec and query not in rec.discovered_via:
                        rec.discovered_via.append(query)
                        storage.upsert_influencer(rec)
                    continue
                rec = found.get(cid)
                if rec is None:
                    rec = Influencer(
                        channel_id=cid,
                        name=sn.get("channelTitle") or cid,
                        profile_url=f"https://www.youtube.com/channel/{cid}",
                        discovered_at=now_iso(),
                    )
                    found[cid] = rec
                if query not in rec.discovered_via:
                    rec.discovered_via.append(query)
            log.info("  search '%s' -> %d results, %d new channels so far", query, len(items), len(found))
            if not token:
                break
        if stopped_reason:
            log.warning("Discovery stopped early: %s", stopped_reason)
            break

    for rec in found.values():
        storage.upsert_influencer(rec)

    return {
        "new_channels": len(found),
        "total_channels": len(existing) + len(found),
        "searches_run": queries_run,
        "quota_units_used": yt.units_used,
        "stopped_reason": stopped_reason,
    }


def apply_channel_resource(rec: Influencer, item: dict[str, Any]) -> None:
    sn = item.get("snippet", {})
    st = item.get("statistics", {})
    branding = item.get("brandingSettings", {}).get("channel", {})
    rec.name = sn.get("title") or rec.name
    rec.handle = sn.get("customUrl")
    if rec.handle:
        handle = rec.handle if rec.handle.startswith("@") else f"@{rec.handle}"
        rec.profile_url = f"https://www.youtube.com/{handle}"
    rec.country = sn.get("country") or branding.get("country")
    rec.default_language = sn.get("defaultLanguage") or branding.get("defaultLanguage")
    rec.channel_created_at = sn.get("publishedAt")
    rec.description = sn.get("description", "") or ""
    rec.channel_keywords = branding.get("keywords", "") or ""
    rec.topic_categories = [u.rsplit("/", 1)[-1].replace("_", " ") for u in item.get("topicDetails", {}).get("topicCategories", [])]
    rec.made_for_kids = item.get("status", {}).get("madeForKids")
    rec.subscribers_hidden = bool(st.get("hiddenSubscriberCount"))
    rec.subscribers = None if rec.subscribers_hidden else safe_int(st.get("subscriberCount"))
    rec.total_views = safe_int(st.get("viewCount"))
    rec.video_count = safe_int(st.get("videoCount"))
    rec.tier = tier_for(rec.subscribers)


def apply_contacts(rec: Influencer) -> None:
    """Extract emails/socials from the channel description first, then video descriptions."""
    if not (rec.email_source or "").startswith("website"):
        rec.email, rec.email_source = NOT_FOUND, None
    sources =[("channel description", rec.description)]
    sources += [(f"video description: {v['title'][:60]}", v.get("description", "")) for v in rec.recent_videos]

    all_emails: list[str] = []
    for label, text in sources:
        for email in extract_emails(text):
            if email not in all_emails:
                all_emails.append(email)
                if rec.email == NOT_FOUND:
                    rec.email = email
                    rec.email_source = label
    rec.other_emails = [e for e in all_emails if e != rec.email]

    combined = "\n".join(text for _, text in sources)
    channel_links = extract_social_links(rec.description)
    any_links = extract_social_links(combined)
    rec.instagram_url = channel_links["instagram"] or any_links["instagram"]
    rec.tiktok_url = channel_links["tiktok"] or any_links["tiktok"]
    rec.twitter_url = channel_links["twitter"] or any_links["twitter"]
    rec.linkedin_url = channel_links["linkedin"] or any_links["linkedin"]
    # Website only from the channel description: video descriptions are full of affiliate links
    rec.website = channel_links["website"]


def collect(cfg: dict[str, Any], yt: YouTubeClient, storage: Storage, force: bool = False) -> dict[str, Any]:
    log = get_logger()
    ccfg = cfg.get("collection", {})
    stages = None if force else [STAGE_DISCOVERED]
    pending = storage.list_influencers(stages=stages)
    if not pending:
        return {"collected": 0, "errors": 0, "quota_units_used": yt.units_used}

    try:
        resources = yt.get_channels([p.channel_id for p in pending])
    except (QuotaBudgetExceeded, QuotaExceeded) as exc:
        log.error("Cannot collect channel stats: %s", exc)
        return {"collected": 0, "errors": len(pending), "quota_units_used": yt.units_used, "stopped_reason": str(exc)}

    collected = errors = 0
    for i, rec in enumerate(pending, 1):
        rec.errors = []
        item = resources.get(rec.channel_id)
        if item is None:
            rec.errors.append("Channel not returned by the API (deleted, terminated or private)")
            rec.stage = STAGE_COLLECTED
            storage.upsert_influencer(rec)
            errors += 1
            continue
        try:
            apply_channel_resource(rec, item)
            uploads = item.get("contentDetails", {}).get("relatedPlaylists", {}).get("uploads")
            videos: list[dict[str, Any]] = []
            if uploads:
                ids = yt.get_upload_video_ids(uploads, int(ccfg.get("recent_videos_to_fetch", 12)))
                videos = [video_from_api(v) for v in yt.get_videos(ids)] if ids else []
            rec.recent_videos = videos
            metrics = compute_metrics(
                videos,
                rec.subscribers,
                videos_for_engagement=int(ccfg.get("videos_for_engagement", 10)),
                min_video_age_days=int(ccfg.get("min_video_age_days", 3)),
            )
            for k, v in metrics.items():
                setattr(rec, k, v)
            apply_contacts(rec)
            collected += 1
        except (QuotaBudgetExceeded, QuotaExceeded) as exc:
            log.error("Quota reached during collection; %d channels left for the next run. %s", len(pending) - i + 1, exc)
            break
        except YouTubeError as exc:
            rec.errors.append(f"Video stats unavailable: {exc}")
            errors += 1
        rec.stage = STAGE_COLLECTED
        storage.upsert_influencer(rec)
        if i % 25 == 0:
            log.info("  collected %d/%d channels (quota used %d)", i, len(pending), yt.units_used)

    return {"collected": collected, "errors": errors, "quota_units_used": yt.units_used}
