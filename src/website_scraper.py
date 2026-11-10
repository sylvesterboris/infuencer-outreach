"""Polite contact-email lookup on a creator's own website.

Only pages the creator linked from their channel are visited, robots.txt is respected,
at most a few pages are fetched, and only addresses actually printed on the page
(text or mailto: links) are returned.
"""
from __future__ import annotations

import re
from urllib import robotparser
from urllib.parse import unquote, urljoin, urlparse

import requests

from .extractors import extract_emails
from .utils import get_logger

USER_AGENT = "InfluencerOutreachResearchBot/1.0 (+contact page lookup; respects robots.txt)"
CONTACT_PATHS = ["", "contact", "contact-us", "about", "collab", "work-with-me", "business"]
MAILTO_RE = re.compile(r"mailto:([^\"'?>\s]+)", re.IGNORECASE)


def _allowed(url: str, session: requests.Session, timeout: float) -> bool:
    parts = urlparse(url)
    robots_url = f"{parts.scheme}://{parts.netloc}/robots.txt"
    rp = robotparser.RobotFileParser()
    try:
        resp = session.get(robots_url, timeout=timeout, headers={"User-Agent": USER_AGENT})
        if resp.status_code >= 400:
            return True  # no robots.txt -> allowed
        rp.parse(resp.text.splitlines())
        return rp.can_fetch(USER_AGENT, url)
    except requests.RequestException:
        return True


def find_email_on_website(
    url: str, session: requests.Session | None = None, timeout: float = 8, max_pages: int = 3
) -> tuple[str | None, str | None]:
    """Return (email, page_url) or (None, None)."""
    log = get_logger()
    session = session or requests.Session()
    if not url.startswith("http"):
        url = "https://" + url
    parts = urlparse(url)
    base = f"{parts.scheme}://{parts.netloc}/"

    candidates = [url] + [urljoin(base, p) for p in CONTACT_PATHS]
    seen: set[str] = set()
    fetched = 0
    for page in candidates:
        key = page.rstrip("/")
        if key in seen or fetched >= max_pages:
            continue
        seen.add(key)
        if not _allowed(page, session, timeout):
            log.debug("robots.txt disallows %s", page)
            continue
        try:
            resp = session.get(page, timeout=timeout, headers={"User-Agent": USER_AGENT}, allow_redirects=True)
        except requests.RequestException as exc:
            log.debug("Website fetch failed %s: %s", page, exc)
            fetched += 1
            continue
        fetched += 1
        if resp.status_code != 200 or "html" not in resp.headers.get("content-type", "html"):
            continue
        html = resp.text[:500_000]
        mailtos = " ".join(unquote(m) for m in MAILTO_RE.findall(html))
        emails = extract_emails(mailtos) or extract_emails(re.sub(r"<[^>]+>", " ", html))
        if emails:
            return emails[0], page
    return None, None
