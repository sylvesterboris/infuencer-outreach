"""Extract contact emails and social/website links from public text.

Emails are only ever *extracted* from text the creator published (channel description,
video descriptions, their own website). Nothing is guessed or pattern-generated.
"""
from __future__ import annotations

import re
from urllib.parse import urlparse

EMAIL_RE = re.compile(r"(?<![\w.+-])([A-Za-z0-9][A-Za-z0-9._%+-]{0,63}@[A-Za-z0-9.-]+\.[A-Za-z]{2,24})(?![\w-])")

# "name [at] domain [dot] com", "name (at) domain (dot) com", "name{at}domain{dot}com"
OBFUSCATED_RE = re.compile(
    r"([A-Za-z0-9._%+-]+)\s*[\[\(\{]\s*at\s*[\]\)\}]\s*([A-Za-z0-9-]+(?:\s*[\[\(\{]\s*dot\s*[\]\)\}]\s*[A-Za-z0-9-]+)+)",
    re.IGNORECASE,
)
DOT_TOKEN_RE = re.compile(r"\s*[\[\(\{]\s*dot\s*[\]\)\}]\s*", re.IGNORECASE)

STRICT_EMAIL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._%+-]{0,63}@(?:[A-Za-z0-9-]+\.)+[A-Za-z]{2,24}$")

JUNK_EMAIL_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".css", ".js")
JUNK_EMAIL_DOMAINS = {
    "example.com",
    "example.org",
    "domain.com",
    "email.com",
    "sentry.io",
    "wixpress.com",
    "sentry-next.wixpress.com",
    "yourdomain.com",
}
JUNK_LOCAL_PARTS = {"noreply", "no-reply", "donotreply", "do-not-reply", "example", "test", "user", "username"}

# Keywords that signal a business/collab address; such emails are preferred
BUSINESS_HINTS = ("business", "collab", "sponsor", "brand", "partner", "enquir", "inquir", "contact", "work", "promo")

URL_RE = re.compile(r"https?://[^\s<>\"'()\[\]]+", re.IGNORECASE)
BARE_SOCIAL_RE = re.compile(
    r"(?<![\w/.])((?:www\.)?(?:instagram\.com|tiktok\.com|twitter\.com|x\.com|linkedin\.com)/[^\s<>\"'()\[\]]+)",
    re.IGNORECASE,
)

INSTAGRAM_SKIP = {"p", "reel", "reels", "explore", "stories", "tv", "accounts", "direct"}
NON_WEBSITE_DOMAINS = (
    "youtube.com",
    "youtu.be",
    "instagram.com",
    "facebook.com",
    "fb.com",
    "fb.me",
    "twitter.com",
    "x.com",
    "tiktok.com",
    "linkedin.com",
    "t.me",
    "telegram.me",
    "wa.me",
    "whatsapp.com",
    "chat.whatsapp.com",
    "discord.gg",
    "discord.com",
    "amzn.to",
    "amazon.",
    "flipkart.com",
    "fkrt.it",
    "bit.ly",
    "goo.gl",
    "t.co",
    "tinyurl.com",
    "play.google.com",
    "apps.apple.com",
    "spotify.com",
    "patreon.com",
    "buymeacoffee.com",
    "github.com",
    "google.com",
    "forms.gle",
    "docs.google.com",
    "drive.google.com",
    "geni.us",
    "amzlink.to",
    "clnk.in",
    "ekaro.in",
    "earnkaro.com",
)


def is_valid_email(email: str) -> bool:
    if not email or not STRICT_EMAIL_RE.match(email):
        return False
    local, domain = email.lower().rsplit("@", 1)
    if domain in JUNK_EMAIL_DOMAINS or local in JUNK_LOCAL_PARTS:
        return False
    if email.lower().endswith(JUNK_EMAIL_SUFFIXES):
        return False
    if ".." in email or domain.startswith("-"):
        return False
    return True


def extract_emails(text: str) -> list[str]:
    """Return unique valid emails found in text, business-looking ones first."""
    if not text:
        return []
    found: list[tuple[int, int, str]] = []  # (priority, position, email)

    for m in EMAIL_RE.finditer(text):
        found.append((0, m.start(), m.group(1).strip(".").lower()))
    for m in OBFUSCATED_RE.finditer(text):
        domain = DOT_TOKEN_RE.sub(".", m.group(2))
        found.append((0, m.start(), f"{m.group(1)}@{domain}".lower()))

    lowered = text.lower()
    seen: set[str] = set()
    scored: list[tuple[int, int, str]] = []
    for _, pos, email in found:
        if email in seen or not is_valid_email(email):
            continue
        seen.add(email)
        window = lowered[max(0, pos - 60) : pos + len(email) + 10]
        local = email.split("@")[0]
        business = any(h in window for h in BUSINESS_HINTS) or any(h in local for h in BUSINESS_HINTS)
        scored.append((0 if business else 1, pos, email))
    scored.sort()
    return [e for _, _, e in scored]


def _clean_url(url: str) -> str:
    url = url.rstrip(".,;:!?)]}>'\"")
    if not url.lower().startswith("http"):
        url = "https://" + url
    return url


def extract_urls(text: str) -> list[str]:
    if not text:
        return []
    urls = [_clean_url(u) for u in URL_RE.findall(text)]
    urls += [_clean_url(u) for u in BARE_SOCIAL_RE.findall(text)]
    seen: set[str] = set()
    out = []
    for u in urls:
        key = u.lower().rstrip("/")
        if key not in seen:
            seen.add(key)
            out.append(u)
    return out


def _host(url: str) -> str:
    host = urlparse(url).netloc.lower()
    return host[4:] if host.startswith("www.") else host


def extract_social_links(text: str) -> dict[str, str | None]:
    """Pick the first plausible profile link per platform."""
    links: dict[str, str | None] = {"instagram": None, "tiktok": None, "twitter": None, "linkedin": None, "website": None}
    for url in extract_urls(text):
        host = _host(url)
        path_parts = [p for p in urlparse(url).path.split("/") if p]
        if host.endswith("instagram.com"):
            if links["instagram"] is None and path_parts and path_parts[0].lower() not in INSTAGRAM_SKIP:
                links["instagram"] = f"https://www.instagram.com/{path_parts[0]}/"
        elif host.endswith("tiktok.com"):
            if links["tiktok"] is None and path_parts and path_parts[0].startswith("@"):
                links["tiktok"] = f"https://www.tiktok.com/{path_parts[0]}"
        elif host in ("twitter.com", "x.com", "mobile.twitter.com"):
            if links["twitter"] is None and path_parts and path_parts[0].lower() not in {"intent", "share", "hashtag", "i"}:
                links["twitter"] = f"https://x.com/{path_parts[0]}"
        elif host.endswith("linkedin.com"):
            if links["linkedin"] is None and path_parts and path_parts[0].lower() in {"in", "company"} and len(path_parts) > 1:
                links["linkedin"] = f"https://www.linkedin.com/{path_parts[0]}/{path_parts[1]}"
        elif links["website"] is None and not any(d in host for d in NON_WEBSITE_DOMAINS):
            links["website"] = url
    return links
