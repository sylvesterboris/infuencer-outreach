"""Stage 3: rule-based filtering & classification for the Technology category.

Every rule produces a check row {criterion, passed, value, threshold, note} so the output
shows exactly why each influencer passed or failed. `passed=None` means the criterion
could not be evaluated because the data is not publicly available; such checks never
fail a creator (and are never filled with guesses).
"""
from __future__ import annotations

import re
from typing import Any

from .models import STAGE_COLLECTED, STAGE_FILTERED, Influencer
from .storage import Storage
from .utils import get_logger

# Sub-niche keyword groups used both for relevance scoring and classification.
TECH_SUBNICHES: dict[str, list[str]] = {
    "Gadgets & Reviews": [
        "review", "unboxing", "smartphone", "phone", "iphone", "android", "samsung", "pixel", "oneplus",
        "laptop", "tablet", "gadget", "gadgets", "earbuds", "headphones", "smartwatch", "camera", "tech",
        "budget phone", "best phone", "specs",
    ],
    "Programming & Web Dev": [
        "coding", "programming", "developer", "python", "javascript", "java", "c++", "react", "node",
        "web development", "html", "css", "dsa", "leetcode", "algorithm", "software", "api", "backend",
        "frontend", "full stack", "git", "linux", "code",
    ],
    "AI & Productivity Tools": [
        "ai", "chatgpt", "gpt", "gemini", "claude", "llm", "prompt", "automation", "ai tools",
        "productivity", "notion", "excel", "no-code", "machine learning", "generative ai",
    ],
    "Data Science & ML": [
        "data science", "data analyst", "data analytics", "machine learning", "deep learning", "sql",
        "power bi", "tableau", "pandas", "statistics", "ml",
    ],
    "PC & Hardware": [
        "pc build", "gaming pc", "gpu", "cpu", "rtx", "ryzen", "intel", "motherboard", "ssd", "monitor",
        "keyboard", "setup", "benchmark",
    ],
    "Cybersecurity": [
        "cybersecurity", "cyber security", "ethical hacking", "hacking", "security", "kali", "privacy",
        "vpn", "malware", "phishing", "bug bounty",
    ],
    "EdTech & Learning Tech": [
        "edtech", "teacher", "teachers", "classroom", "students", "online learning", "study tools",
        "education technology", "lms", "google classroom",
    ],
}

TECH_TOPIC_CATEGORIES = {"technology", "video game culture", "knowledge"}
EDUCATIONAL_HINTS = [
    "how to", "tutorial", "explained", "explain", "learn", "course", "for beginners", "beginner",
    "guide", "step by step", "tips", "roadmap", "in hindi", "full course", "crash course", "what is",
]


def _compile(words: list[str]) -> re.Pattern[str]:
    escaped = sorted((re.escape(w) for w in words), key=len, reverse=True)
    return re.compile(r"(?<![a-z0-9])(?:" + "|".join(escaped) + r")(?![a-z0-9])", re.IGNORECASE)


_SUBNICHE_PATTERNS = {name: _compile(words) for name, words in TECH_SUBNICHES.items()}
_ALL_TECH = _compile(sorted({w for ws in TECH_SUBNICHES.values() for w in ws}))
_EDU = _compile(EDUCATIONAL_HINTS)


def relevance_and_subniche(rec: Influencer) -> tuple[float, str | None, float, dict[str, int]]:
    """Return (relevance 0-1, best sub-niche, educational ratio, sub-niche hit counts)."""
    titles = [v.get("title", "") for v in rec.recent_videos]
    tag_text = " ".join(" ".join(v.get("tags", [])) for v in rec.recent_videos)

    title_ratio = sum(1 for t in titles if _ALL_TECH.search(t)) / len(titles) if titles else 0.0
    profile_text = f"{rec.description} {rec.channel_keywords}"
    profile_hits = len(set(m.lower() for m in _ALL_TECH.findall(profile_text)))
    topic_match = any(t.lower() in TECH_TOPIC_CATEGORIES for t in rec.topic_categories)

    score = 0.55 * title_ratio + 0.25 * min(profile_hits / 4, 1.0) + (0.20 if topic_match else 0.0)

    corpus = " ".join(titles) + " " + tag_text + " " + profile_text
    hits = {name: len(p.findall(corpus)) for name, p in _SUBNICHE_PATTERNS.items()}
    best = max(hits, key=hits.get) if any(hits.values()) else None

    edu_ratio = sum(1 for t in titles if _EDU.search(t)) / len(titles) if titles else 0.0
    return round(score, 3), best, round(edu_ratio, 3), hits


def _check(criterion: str, passed: bool | None, value: Any, threshold: Any, note: str = "") -> dict[str, Any]:
    return {"criterion": criterion, "passed": passed, "value": value, "threshold": threshold, "note": note}


def evaluate(rec: Influencer, fcfg: dict[str, Any]) -> Influencer:
    checks: list[dict[str, Any]] = []
    rec.category = fcfg.get("category", "Technology")

    # Data availability
    if rec.errors and not rec.recent_videos and rec.subscribers is None:
        checks.append(_check("Data available", False, "; ".join(rec.errors), "channel + video stats"))

    # Platform
    want_platform = fcfg.get("platform", "YouTube")
    checks.append(_check("Platform", rec.platform == want_platform, rec.platform, want_platform))

    # Follower count
    lo, hi = int(fcfg["min_subscribers"]), int(fcfg["max_subscribers"])
    if rec.subscribers_hidden or rec.subscribers is None:
        checks.append(_check("Subscriber count", False, "Hidden", f"{lo:,}-{hi:,}", "creator hides subscriber count"))
    else:
        ok = lo <= rec.subscribers <= hi
        checks.append(_check("Subscriber count", ok, rec.subscribers, f"{lo:,}-{hi:,}",
                             "" if ok else ("below micro range" if rec.subscribers < lo else "above micro range")))

    # Engagement rate
    min_er = float(fcfg["min_engagement_rate"])
    min_videos = int(fcfg.get("min_videos_analysed", 3))
    if rec.engagement_rate is None or rec.videos_analysed < min_videos:
        checks.append(_check("Engagement rate", False, "Not Available", f">= {min_er:.1%}",
                             f"only {rec.videos_analysed} recent videos with public likes/views (need {min_videos})"))
    else:
        checks.append(_check("Engagement rate", rec.engagement_rate >= min_er, f"{rec.engagement_rate:.2%}",
                             f">= {min_er:.1%}", f"over {rec.videos_analysed} recent videos"))

    # Activity
    max_days = int(fcfg.get("max_days_since_upload", 60))
    if rec.days_since_upload is None:
        checks.append(_check("Recent activity", False, "No uploads found", f"<= {max_days} days"))
    else:
        checks.append(_check("Recent activity", rec.days_since_upload <= max_days,
                             f"{rec.days_since_upload} days since last upload", f"<= {max_days} days"))

    # Category / content relevance
    score, sub, edu, _hits = relevance_and_subniche(rec)
    rec.relevance_score, rec.sub_niche, rec.educational_ratio = score, sub, edu
    min_rel = float(fcfg.get("min_relevance_score", 0.35))
    checks.append(_check("Niche relevance (Technology)", score >= min_rel, score, f">= {min_rel}",
                         f"top sub-niche: {sub or 'none'}"))

    # Geography (creator's channel country - the only public geo signal)
    allowed = [c.upper() for c in fcfg.get("allowed_countries") or []]
    if not allowed:
        checks.append(_check("Geography", True, rec.country or "Not set", "any"))
    elif not rec.country:
        checks.append(_check("Geography", None, "Not set by creator", ", ".join(allowed),
                             "cannot evaluate; not failed"))
    else:
        checks.append(_check("Geography", rec.country.upper() in allowed, rec.country, ", ".join(allowed)))

    # Audience demographics - not public on YouTube; recorded transparently, never guessed
    checks.append(_check("Audience demographics", None, "Not Available", "n/a",
                         "only visible to the channel owner in YouTube Analytics; not evaluated"))

    # Brand fit / safety
    blocklist = fcfg.get("brand_safety_blocklist") or []
    corpus = " ".join([rec.name, rec.description, rec.channel_keywords] + [v.get("title", "") for v in rec.recent_videos])
    flagged = sorted({w for w in blocklist if re.search(r"(?<![a-z0-9])" + re.escape(w.lower()) + r"(?![a-z0-9])", corpus.lower())})
    checks.append(_check("Brand safety", not flagged, ", ".join(flagged) or "clean", "no blocklisted terms",
                         "" if not flagged else "blocklisted terms in titles/description"))
    if fcfg.get("exclude_made_for_kids", True):
        checks.append(_check("Not 'made for kids'", rec.made_for_kids is not True, rec.made_for_kids, "False",
                             "made-for-kids channels have comments disabled and strict ad rules"))

    rec.filter_checks = checks
    rec.fail_reasons = [
        f"{c['criterion']}: {c['value']} (needs {c['threshold']})" + (f" - {c['note']}" if c["note"] else "")
        for c in checks
        if c["passed"] is False
    ]
    rec.filter_status = "FAIL" if rec.fail_reasons else "PASS"
    rec.stage = STAGE_FILTERED
    return rec


def run_filtering(cfg: dict[str, Any], storage: Storage, refilter: bool = False) -> dict[str, int]:
    log = get_logger()
    fcfg = cfg["filtering"]
    stages = [STAGE_COLLECTED, STAGE_FILTERED, "enriched"] if refilter else [STAGE_COLLECTED]
    passed = failed = 0
    for rec in storage.list_influencers(stages=stages):
        was_enriched = rec.stage == "enriched"
        evaluate(rec, fcfg)
        if was_enriched and rec.filter_status == "PASS":
            rec.stage = "enriched"  # keep enrichment if it still qualifies
        storage.upsert_influencer(rec)
        passed += rec.filter_status == "PASS"
        failed += rec.filter_status == "FAIL"
    log.info("Filtering: %d passed, %d failed", passed, failed)
    return {"passed": passed, "failed": failed}
