"""Stage 4: enrich shortlisted influencers.

1. Contact email: if none was found in YouTube descriptions, check the creator's own
   linked website (robots.txt respected). Still nothing -> stays "Not Found".
2. Content context: an LLM reads the channel description + recent titles and returns
   themes, tone, sub-niche and a standout recent video. Output is validated against the
   source data; if the LLM is unavailable a keyword-based fallback is used and labelled.
"""
from __future__ import annotations

import re
from collections import Counter
from typing import Any

import requests

from .filtering import TECH_SUBNICHES, relevance_and_subniche
from .llm import GroqClient, LLMConfigError, LLMError
from .models import NOT_FOUND, STAGE_ENRICHED, STAGE_FILTERED, Influencer
from .storage import Storage
from .utils import get_logger, normalize, parse_iso, utc_now
from .website_scraper import find_email_on_website

ANALYSIS_SYSTEM = """You analyse YouTube channels using only their public metadata.
Describe only what the given titles, tags and description show. Never invent facts,
numbers, sponsors or audience statistics.

Return a JSON object with exactly these keys:
- "content_themes": list of 3-5 short phrases (2-5 words each) naming the recurring topics
- "content_tone": one phrase of at most 8 words describing style/format/language (e.g. "casual Hinglish phone reviews")
- "sub_niche": one value from the allowed list provided
- "inferred_audience": at most 12 words on who the content seems made for (this is an inference from content)
- "standout_recent_video": one title copied EXACTLY from the recent video list (pick the most distinctive or best performing)"""

STOPWORDS = set(
    "the a an and or of to in for on with is are how what why best my your you this that new from vs at by "
    "it its be can i we our full video part 2024 2025 2026 hindi english india shorts short official "
    "like looks look under around more most top first free all just get got not now one only out over "
    "really than then them they very will about after before into here there which who also every "
    "pro max plus ultra mini price launch launched unboxing review reviews vlog viral trending".split()
)


def _video_lines(rec: Influencer, limit: int = 10) -> list[str]:
    now = utc_now()
    lines = []
    for v in rec.recent_videos[:limit]:
        published = parse_iso(v.get("published_at"))
        age = f"{(now - published).days}d ago" if published else "date unknown"
        views = f"{v['views']:,} views" if v.get("views") is not None else "views hidden"
        lines.append(f"- {v.get('title', '').strip()} ({age}, {views})")
    return lines


def build_analysis_prompt(rec: Influencer) -> str:
    tags = Counter(t.lower() for v in rec.recent_videos for t in v.get("tags", []))
    return "\n".join(
        [
            f"Channel name: {rec.name}",
            f"Channel description: {rec.description[:700] or '(empty)'}",
            f"Channel keywords: {rec.channel_keywords[:200] or '(none)'}",
            f"Most used video tags: {', '.join(t for t, _ in tags.most_common(15)) or '(none)'}",
            "Recent videos (newest first):",
            *_video_lines(rec),
            "",
            f"Allowed sub_niche values: {', '.join(TECH_SUBNICHES)}, Other tech",
        ]
    )


def validate_analysis(data: dict[str, Any], rec: Influencer) -> dict[str, Any]:
    """Keep only well-formed fields; drop a standout video that isn't a real title."""
    out: dict[str, Any] = {}
    themes = data.get("content_themes")
    if isinstance(themes, list):
        cleaned = [str(t).strip()[:60] for t in themes if str(t).strip()]
        if 2 <= len(cleaned) <= 6:
            out["content_themes"] = cleaned[:5]
    tone = data.get("content_tone")
    if isinstance(tone, str) and 0 < len(tone.split()) <= 12:
        out["content_tone"] = tone.strip()
    sub = data.get("sub_niche")
    if isinstance(sub, str) and (sub in TECH_SUBNICHES or sub == "Other tech"):
        out["sub_niche"] = sub
    aud = data.get("inferred_audience")
    if isinstance(aud, str) and 0 < len(aud.split()) <= 18:
        out["inferred_audience"] = aud.strip()
    standout = data.get("standout_recent_video")
    titles = {normalize(v.get("title", "")): v.get("title", "") for v in rec.recent_videos}
    if isinstance(standout, str) and normalize(standout) in titles:
        out["standout_video"] = titles[normalize(standout)]
    return out


def keyword_fallback(rec: Influencer) -> dict[str, Any]:
    """Deterministic themes from sub-niche hits and frequent title/tag words."""
    _, sub, _, hits = relevance_and_subniche(rec)
    themes = [name for name, n in sorted(hits.items(), key=lambda kv: -kv[1]) if n > 0][:2]
    words = Counter()
    for v in rec.recent_videos:
        for w in re.findall(r"[a-zA-Z][a-zA-Z0-9+#.]{2,}", v.get("title", "") + " " + " ".join(v.get("tags", []))):
            if w.lower() not in STOPWORDS:
                words[w.lower()] += 1
    themes += [w for w, n in words.most_common(6) if n >= 2 and w not in " ".join(themes).lower()][: 5 - len(themes)]
    best = max(rec.recent_videos, key=lambda v: v.get("views") or 0, default=None)
    return {
        "content_themes": themes or ["Not determined"],
        "sub_niche": sub,
        "standout_video": best.get("title") if best else None,
    }


def enrich_one(
    rec: Influencer,
    ecfg: dict[str, Any],
    llm: GroqClient | None,
    http: requests.Session | None = None,
) -> Influencer:
    log = get_logger()

    # 1) Website contact lookup
    if rec.email == NOT_FOUND and rec.website and ecfg.get("website_email_lookup", True):
        try:
            email, page = find_email_on_website(
                rec.website,
                session=http,
                timeout=float(ecfg.get("website_timeout_seconds", 8)),
                max_pages=int(ecfg.get("website_max_pages", 3)),
            )
        except Exception as exc:  # never let one bad site break the run
            email, page = None, None
            rec.errors.append(f"Website lookup failed: {exc}")
        if email:
            rec.email, rec.email_source = email, f"website: {page}"

    # 2) Content analysis
    analysis: dict[str, Any] = {}
    if llm is not None and ecfg.get("use_llm_content_analysis", True):
        rec.errors = [e for e in rec.errors if not e.startswith("LLM analysis failed")]
        try:
            raw = llm.complete_json(ANALYSIS_SYSTEM, build_analysis_prompt(rec), temperature=0.2)
            analysis = validate_analysis(raw, rec)
        except LLMConfigError:
            raise  # misconfiguration: let the stage stop using the LLM
        except LLMError as exc:
            log.warning("LLM analysis failed for %s: %s", rec.name, exc)
            rec.errors.append(f"LLM analysis failed: {exc}")

    if analysis.get("content_themes"):
        rec.themes_source = "llm"
    else:
        analysis = {**keyword_fallback(rec), **analysis}
        rec.themes_source = "keyword-fallback"

    rec.content_themes = analysis.get("content_themes", rec.content_themes)
    rec.content_tone = analysis.get("content_tone", rec.content_tone)
    rec.sub_niche = analysis.get("sub_niche") or rec.sub_niche
    rec.inferred_audience = analysis.get("inferred_audience", rec.inferred_audience)
    rec.standout_video = analysis.get("standout_video") or rec.standout_video
    rec.stage = STAGE_ENRICHED
    return rec


def run_enrichment(
    cfg: dict[str, Any], storage: Storage, llm: GroqClient | None, force: bool = False
) -> dict[str, int]:
    log = get_logger()
    ecfg = cfg.get("enrichment", {})
    use_llm = llm is not None and ecfg.get("use_llm_content_analysis", True)
    todo = [
        rec
        for rec in storage.list_influencers(stages=[STAGE_FILTERED, STAGE_ENRICHED], filter_status="PASS")
        if force
        or rec.stage == STAGE_FILTERED
        # an earlier run fell back to keyword themes (LLM down/misconfigured): retry with the LLM
        or (use_llm and rec.themes_source == "keyword-fallback")
    ]
    http = requests.Session()
    done = with_email = 0
    for i, rec in enumerate(todo, 1):
        try:
            enrich_one(rec, ecfg, llm, http)
        except LLMConfigError as exc:
            log.error("LLM unusable, falling back to keyword themes for the rest of this stage: %s", exc)
            llm = None
            enrich_one(rec, ecfg, None, http)
        storage.upsert_influencer(rec)
        done += 1
        with_email += rec.email != NOT_FOUND
        log.info("  enriched %d/%d: %s (email: %s, themes: %s)", i, len(todo), rec.name,
                 "found" if rec.email != NOT_FOUND else "Not Found", rec.themes_source)
    return {"enriched": done, "with_email": with_email}
