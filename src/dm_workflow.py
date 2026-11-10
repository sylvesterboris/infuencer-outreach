"""Instagram DM workflow (manual by design).

Meta's Instagram Messaging API only lets a business account reply to people who messaged it
first; it does not allow cold DMs to creators. So the pipeline does not automate sending.
Instead it builds a DM queue (profile link + ready-to-paste text) and a person marks each
DM as sent. Duplicates are blocked the same way as email.
"""
from __future__ import annotations

from typing import Any

from .models import NOT_FOUND
from .storage import Storage
from .utils import word_count

DM_CHANNEL = "instagram_dm"
DM_DONE = {"SENT_MANUALLY", "SKIPPED_MANUALLY"}


def build_dm_queue(storage: Storage) -> dict[str, int]:
    queued = already = no_profile = 0
    for rec in storage.list_influencers(filter_status="PASS"):
        msg = storage.get_message(rec.channel_id)
        if not msg or not msg.get("dm_text"):
            continue
        last = storage.latest_outreach(rec.channel_id, DM_CHANNEL)
        if last and last["status"] in DM_DONE | {"PENDING_MANUAL"}:
            already += 1
            continue
        storage.log_outreach(
            rec.channel_id, rec.name, DM_CHANNEL, "PENDING_MANUAL",
            recipient=rec.instagram_url or None, mode="manual",
            error=None if rec.instagram_url else "Instagram profile Not Found in public links",
        )
        queued += 1
        no_profile += not rec.instagram_url
    return {"queued": queued, "already_queued_or_done": already, "without_instagram_link": no_profile}


def mark_dm(storage: Storage, channel_id: str, status: str = "SENT_MANUALLY") -> str:
    if status not in DM_DONE:
        raise ValueError(f"status must be one of {sorted(DM_DONE)}")
    rec = storage.get_influencer(channel_id)
    if rec is None:
        raise ValueError(f"Unknown channel id {channel_id}")
    last = storage.latest_outreach(channel_id, DM_CHANNEL)
    if last and last["status"] == "SENT_MANUALLY":
        return f"{rec.name}: DM already marked as sent on {last['created_at'][:10]} - not logging a duplicate"
    storage.log_outreach(channel_id, rec.name, DM_CHANNEL, status, recipient=rec.instagram_url, mode="manual")
    return f"{rec.name}: DM marked {status}"


def dm_rows(storage: Storage) -> list[dict[str, Any]]:
    rows = []
    for rec in storage.list_influencers(filter_status="PASS"):
        msg = storage.get_message(rec.channel_id)
        if not msg or not msg.get("dm_text"):
            continue
        last = storage.latest_outreach(rec.channel_id, DM_CHANNEL)
        rows.append(
            {
                "Channel ID": rec.channel_id,
                "Influencer": rec.name,
                "Instagram": rec.instagram_url or NOT_FOUND,
                "YouTube": rec.profile_url,
                "DM Text": msg["dm_text"],
                "DM Words": word_count(msg["dm_text"]),
                "Status": last["status"] if last else "NOT_QUEUED",
                "Updated": last["created_at"] if last else "",
            }
        )
    return rows
