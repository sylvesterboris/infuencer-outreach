"""Stage 5: AI personalization - an email pitch (60-90 words) and an Instagram DM (15-30 words).

Personalization is dynamic at two levels:
* A rule-based angle picker chooses the collaboration type per creator from their metrics
  and content (shorts-heavy -> UGC, review channel -> product placement, ...).
* The LLM writes both messages from the creator's real signals (recent titles, themes, tone).

Every draft is validated (word counts, mentions the channel, references a real content
signal, no placeholders, no invented numbers, no clichés). Failed drafts are regenerated
with the problems fed back; drafts that still fail are stored as `needs_review`.
"""
from __future__ import annotations

import json
import re
from typing import Any

from .llm import GroqClient, LLMConfigError, LLMError
from .models import STAGE_ENRICHED, Influencer
from .storage import Storage
from .utils import get_logger, normalize, parse_iso, utc_now, word_count

PROMPT_VERSION = "outreach-v1"

MESSAGE_SYSTEM = """You write short creator-outreach messages for {brand}. You sound like a real
person on a small partnerships team who has actually looked at the channel: plain words, specific,
friendly, zero hype.

Rules:
1. Address the creator using the channel name exactly as given. Never invent a personal name.
2. Mention at least one concrete thing from their content: a recent video title or one of their themes.
3. Use only facts from the data you are given. Do not invent statistics, follower numbers, dates,
   results, or past collaborations.
4. Pitch the collaboration angle you are given and say briefly why it suits their audience.
5. End the email with one easy next step (a question or a quick call), no pressure.
6. email_body: {email_min}-{email_max} words. No subject line, no greeting-only lines, no signature,
   no placeholders like [Name].
7. dm: {dm_min}-{dm_max} words, casual Instagram tone, 1-2 sentences, no hashtags, no links.
8. Avoid these phrases: {banned}.

Return a JSON object: {{"email_subject": "...", "email_body": "...", "dm": "..."}}
The subject should be under 9 words and specific to this creator."""

PLACEHOLDER_RE = re.compile(r"\[[^\]]*\]|\{[^}]*\}|<[^>]+>|\bXX+\b|\bYOUR NAME\b", re.IGNORECASE)
NUMBER_RE = re.compile(r"\d[\d,.]*")
SIGNAL_STOPWORDS = set(
    "the a an and or of to in for on with is are how what why best my your you this that new from vs at by it "
    "its be can i we our full video part hindi english india shorts short tips guide review tutorial "
    "explained beginners beginner".split()
)


# --------------------------------------------------------------------------- angle selection
def choose_angle(rec: Influencer) -> dict[str, str]:
    subs = rec.subscribers or 0
    er = rec.engagement_rate or 0
    edu = rec.educational_ratio or 0
    shorts = rec.shorts_share or 0

    if shorts >= 0.6:
        return {
            "type": "UGC content creation",
            "description": "a few short-form videos showing a quick, real use of our tools, which we would also run as ads",
            "why": f"{shorts:.0%} of recent uploads are Shorts",
        }
    if rec.sub_niche == "Gadgets & Reviews" and edu < 0.4:
        return {
            "type": "Paid product placement",
            "description": "a paid hands-on walkthrough segment of our tools inside one of your upcoming videos",
            "why": "review-style channel",
        }
    if er >= 0.04 and subs < 30_000:
        return {
            "type": "Brand ambassador program",
            "description": "a 3-month ambassador partnership: one video a month plus a personal referral link",
            "why": f"high engagement ({er:.1%}) on a tight-knit audience",
        }
    if subs < 15_000:
        return {
            "type": "Affiliate campaign",
            "description": "an affiliate partnership with a tracked referral link and free premium access for you",
            "why": "emerging channel; low-risk performance-based start",
        }
    if edu >= 0.4 or rec.sub_niche in {"EdTech & Learning Tech", "Programming & Web Dev", "AI & Productivity Tools"}:
        return {
            "type": "Sponsorship",
            "description": "a paid sponsored integration inside one of your tutorials or explainers",
            "why": "educational content matches an EdTech brand",
        }
    return {
        "type": "Sponsorship",
        "description": "a paid sponsored segment in an upcoming video",
        "why": "default for established micro channels",
    }


# --------------------------------------------------------------------------- prompt
def build_signals(rec: Influencer, brand: dict[str, Any], angle: dict[str, str]) -> dict[str, Any]:
    now = utc_now()
    recent = []
    for v in rec.recent_videos[:3]:
        published = parse_iso(v.get("published_at"))
        recent.append({"title": v.get("title", ""), "posted": f"{(now - published).days} days ago" if published else None})
    return {
        "channel_name": rec.name,
        "platform": rec.platform,
        "niche": rec.category,
        "sub_niche": rec.sub_niche,
        "content_themes": rec.content_themes,
        "content_tone": rec.content_tone,
        "recent_videos": recent,
        "standout_video": rec.standout_video,
        "seems_made_for": rec.inferred_audience,
        "creator_country": rec.country,
        "collaboration_angle": {"type": angle["type"], "what_we_propose": angle["description"]},
        "brand": {"name": brand.get("name"), "about": brand.get("one_liner"), "offer": brand.get("offer")},
    }


def _signal_words(rec: Influencer) -> set[str]:
    texts = [v.get("title", "") for v in rec.recent_videos[:5]] + list(rec.content_themes or [])
    if rec.standout_video:
        texts.append(rec.standout_video)
    words = set()
    for t in texts:
        for w in normalize(t).split():
            if len(w) >= 4 and w not in SIGNAL_STOPWORDS and not w.isdigit():
                words.add(w)
    return words


def validate_messages(msg: dict[str, Any], rec: Influencer, signals_json: str, mcfg: dict[str, Any]) -> list[str]:
    issues: list[str] = []
    subject = str(msg.get("email_subject") or "").strip()
    body = str(msg.get("email_body") or "").strip()
    dm = str(msg.get("dm") or "").strip()
    e_min, e_max = mcfg.get("email_words", [60, 90])
    d_min, d_max = mcfg.get("dm_words", [15, 30])

    if not subject:
        issues.append("email_subject is empty")
    ew, dw = word_count(body), word_count(dm)
    if not e_min <= ew <= e_max:
        issues.append(f"email_body has {ew} words; it must be {e_min}-{e_max}")
    if not d_min <= dw <= d_max:
        issues.append(f"dm has {dw} words; it must be {d_min}-{d_max}")

    name_norm = normalize(rec.name)
    if name_norm:
        if name_norm not in normalize(body):
            issues.append(f"email_body must address the creator as '{rec.name}'")
        if name_norm not in normalize(dm):
            issues.append(f"dm must address the creator as '{rec.name}'")

    signal_words = _signal_words(rec)
    if signal_words:
        if not signal_words & set(normalize(body).split()):
            issues.append("email_body does not reference any recent video or content theme")
        if not signal_words & set(normalize(dm).split()):
            issues.append("dm does not reference any recent video or content theme")

    for label, text in (("email_subject", subject), ("email_body", body), ("dm", dm)):
        if PLACEHOLDER_RE.search(text):
            issues.append(f"{label} contains a placeholder")
        for num in NUMBER_RE.findall(text):
            if num.strip(".,") and num.strip(".,") not in signals_json:
                issues.append(f"{label} contains the number '{num}' which is not in the data (possible invented fact)")
        lowered = text.lower()
        for phrase in mcfg.get("banned_phrases", []):
            if phrase.lower() in lowered:
                issues.append(f"{label} uses the banned phrase '{phrase}'")
    if "#" in dm:
        issues.append("dm must not contain hashtags")
    if "http" in dm.lower() or "www." in dm.lower():
        issues.append("dm must not contain links")
    return issues


def generate_for(
    rec: Influencer, llm: GroqClient, cfg: dict[str, Any]
) -> dict[str, Any]:
    brand = cfg["brand"]
    mcfg = cfg.get("messages", {})
    angle = choose_angle(rec)
    signals = build_signals(rec, brand, angle)
    signals_json = json.dumps(signals, ensure_ascii=False)
    e_min, e_max = mcfg.get("email_words", [60, 90])
    d_min, d_max = mcfg.get("dm_words", [15, 30])
    system = MESSAGE_SYSTEM.format(
        brand=brand.get("name", "our brand"),
        email_min=e_min,
        email_max=e_max,
        dm_min=d_min,
        dm_max=d_max,
        banned=", ".join(f'"{p}"' for p in mcfg.get("banned_phrases", [])),
    )
    base_user = f"Creator data (JSON):\n{signals_json}"

    best: tuple[int, dict[str, Any], list[str]] | None = None
    attempts = 0
    feedback = ""
    for attempt in range(1, int(cfg.get("llm", {}).get("max_attempts", 3)) + 1):
        attempts = attempt
        user = base_user + (f"\n\nYour previous draft had these problems, fix all of them:\n{feedback}" if feedback else "")
        try:
            msg = llm.complete_json(system, user)
        except LLMConfigError:
            raise
        except LLMError:
            if best is None:
                raise  # nothing usable yet
            break  # keep the best draft we already have
        issues = validate_messages(msg, rec, signals_json, mcfg)
        if best is None or len(issues) < best[0]:
            best = (len(issues), msg, issues)
        if not issues:
            break
        feedback = "\n".join(f"- {i}" for i in issues)

    assert best is not None
    _, msg, issues = best
    return {
        "email_subject": str(msg.get("email_subject") or "").strip(),
        "email_body": str(msg.get("email_body") or "").strip(),
        "dm_text": str(msg.get("dm") or "").strip(),
        "angle": f"{angle['type']} ({angle['why']})",
        "model": llm.model,
        "prompt_version": PROMPT_VERSION,
        "attempts": attempts,
        "validation_status": "ok" if not issues else "needs_review",
        "validation_notes": "; ".join(issues) or None,
    }


def run_personalization(
    cfg: dict[str, Any], storage: Storage, llm: GroqClient, regenerate: bool = False
) -> dict[str, int]:
    log = get_logger()
    todo = storage.list_influencers(stages=[STAGE_ENRICHED], filter_status="PASS")
    generated = needs_review = failed = skipped = 0
    for rec in todo:
        existing = storage.get_message(rec.channel_id)
        # a previous attempt that errored (LLM down, rate limit, bad model) is always retried
        if existing and existing.get("validation_status") != "failed" and not regenerate:
            skipped += 1
            continue
        if existing and existing.get("review_status") == "rejected":
            skipped += 1  # a human rejected this creator; don't resurrect it
            continue
        if existing and storage.already_contacted("email", ["SENT"], rec.channel_id):
            skipped += 1  # never rewrite a message that was really sent
            continue
        try:
            result = generate_for(rec, llm, cfg)
        except LLMConfigError as exc:
            log.error("Stopping personalization, the LLM is misconfigured: %s", exc)
            raise
        except LLMError as exc:
            log.warning("Message generation failed for %s: %s", rec.name, exc)
            storage.save_message(rec.channel_id, validation_status="failed", validation_notes=str(exc),
                                 model=llm.model, prompt_version=PROMPT_VERSION)
            failed += 1
            continue
        storage.save_message(rec.channel_id, **result)
        generated += 1
        needs_review += result["validation_status"] != "ok"
        log.info("  messages for %s: %s (%d attempt%s)", rec.name, result["validation_status"],
                 result["attempts"], "" if result["attempts"] == 1 else "s")
    return {"generated": generated, "needs_review": needs_review, "failed": failed, "skipped": skipped}
