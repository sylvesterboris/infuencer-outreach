"""Loads config.yaml and merges secrets from the environment (.env)."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config.yaml"


class ConfigError(Exception):
    """Raised when configuration or a required secret is missing."""


def load_config(path: str | os.PathLike | None = None) -> dict[str, Any]:
    load_dotenv(PROJECT_ROOT / ".env")
    cfg_path = Path(path) if path else DEFAULT_CONFIG_PATH
    if not cfg_path.exists():
        raise ConfigError(f"Config file not found: {cfg_path}")

    with open(cfg_path, encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh) or {}

    base = cfg_path.resolve().parent
    paths = cfg.setdefault("paths", {})
    paths["output_dir"] = str((base / paths.get("output_dir", "outputs")).resolve())
    paths["database"] = str((base / paths.get("database", "outputs/pipeline.db")).resolve())
    Path(paths["output_dir"]).mkdir(parents=True, exist_ok=True)

    smtp_user = os.getenv("SMTP_USER", "")
    cfg["secrets"] = {
        "youtube_api_key": os.getenv("YOUTUBE_API_KEY", "").strip(),
        "groq_api_key": os.getenv("GROQ_API_KEY", "").strip(),
        "openai_api_key": os.getenv("OPENAI_API_KEY", "").strip(),
        "xai_api_key": os.getenv("XAI_API_KEY", "").strip(),
        "smtp_host": os.getenv("SMTP_HOST", "smtp.gmail.com"),
        "smtp_port": int(os.getenv("SMTP_PORT", "465") or 465),
        "smtp_user": smtp_user,
        "smtp_password": os.getenv("SMTP_PASSWORD", ""),
        "smtp_from": os.getenv("SMTP_FROM", "") or smtp_user,
    }
    return cfg


def require_secret(cfg: dict[str, Any], key: str, purpose: str) -> str:
    value = cfg.get("secrets", {}).get(key, "")
    if not value:
        env_name = key.upper()
        raise ConfigError(f"{env_name} is not set (needed for {purpose}). Add it to your .env file.")
    return value
