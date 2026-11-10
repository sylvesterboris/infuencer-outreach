"""Small shared helpers: time, text, logging."""
from __future__ import annotations

import logging
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

_DURATION_RE = re.compile(
    r"^P(?:(?P<days>\d+)D)?(?:T(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?(?:(?P<seconds>\d+)S)?)?$"
)
_WORD_RE = re.compile(r"[A-Za-z0-9ऀ-ॿ][A-Za-z0-9ऀ-ॿ'’\-]*")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def now_iso() -> str:
    return utc_now().replace(microsecond=0).isoformat()


def parse_iso(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def parse_iso8601_duration(value: str | None) -> int | None:
    """'PT4M13S' -> 253 seconds. Returns None for missing/unknown formats (e.g. live 'P0D')."""
    if not value:
        return None
    m = _DURATION_RE.match(value)
    if not m:
        return None
    parts = {k: int(v) for k, v in m.groupdict().items() if v}
    return (
        parts.get("days", 0) * 86400
        + parts.get("hours", 0) * 3600
        + parts.get("minutes", 0) * 60
        + parts.get("seconds", 0)
    )


def word_count(text: str) -> int:
    return len(_WORD_RE.findall(text or ""))


def normalize(text: str) -> str:
    """Lowercase alphanumeric-only form, used for loose matching."""
    return re.sub(r"[^a-z0-9ऀ-ॿ]+", " ", (text or "").lower()).strip()


def safe_int(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def setup_logging(output_dir: str, verbose: bool = False) -> logging.Logger:
    logger = logging.getLogger("outreach")
    if logger.handlers:
        return logger
    logger.setLevel(logging.DEBUG)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%H:%M:%S")

    console = logging.StreamHandler(sys.stdout)
    console.setLevel(logging.DEBUG if verbose else logging.INFO)
    console.setFormatter(fmt)
    logger.addHandler(console)

    log_file = Path(output_dir) / "pipeline.log"
    fh = logging.FileHandler(log_file, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)
    logger.addHandler(fh)
    return logger


def get_logger() -> logging.Logger:
    return logging.getLogger("outreach")
