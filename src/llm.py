"""LLM client for OpenAI-compatible chat-completions endpoints (Groq by default, also OpenAI / xAI)."""
from __future__ import annotations

import json
import re
import time
from typing import Any

import requests

from .utils import get_logger

PROVIDER_URLS = {
    "groq": "https://api.groq.com/openai/v1/chat/completions",
    "openai": "https://api.openai.com/v1/chat/completions",
    "xai": "https://api.x.ai/v1/chat/completions",
}
DEFAULT_PROVIDER = "groq"
DEFAULT_MODEL = "qwen/qwen3.8-27b"

_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
_FENCE_RE = re.compile(r"^```[a-zA-Z]*\s*|\s*```$")


class LLMError(Exception):
    """A single request failed (rate limit, timeout, bad output). Safe to skip and retry later."""


class LLMConfigError(LLMError):
    """The request can never succeed as configured (bad key, unknown model, no access).

    Stages stop calling the LLM when they see this instead of failing every record one by one.
    """


def parse_json_content(content: str) -> dict[str, Any]:
    """Parse a model reply into a JSON object, tolerating <think> blocks, code fences and chatter."""
    text = _THINK_RE.sub("", content or "").strip()
    text = _FENCE_RE.sub("", text).strip()
    try:
        data = json.loads(text)
    except ValueError:
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            raise
        data = json.loads(text[start : end + 1])
    if not isinstance(data, dict):
        raise ValueError("response is not a JSON object")
    return data


class GroqClient:
    """Chat-completions client. Named for the default provider; works with any in PROVIDER_URLS."""

    def __init__(
        self,
        api_key: str,
        model: str = DEFAULT_MODEL,
        temperature: float = 0.7,
        min_interval: float = 1.0,
        timeout: float = 60,
        max_retries: int = 6,
        session: requests.Session | None = None,
        provider: str = DEFAULT_PROVIDER,
    ):
        if provider not in PROVIDER_URLS:
            raise LLMConfigError(f"Unknown LLM provider '{provider}' (use one of {sorted(PROVIDER_URLS)})")
        self.api_key = api_key
        self.model = model
        self.provider = provider
        self.temperature = temperature
        self.min_interval = min_interval
        self.timeout = timeout
        self.max_retries = max_retries
        self.session = session or requests.Session()
        self.url = PROVIDER_URLS[provider]
        self.json_mode = True  # switched off automatically if the model rejects response_format
        self._last_call = 0.0
        self.calls = 0
        self.log = get_logger()

    def _throttle(self) -> None:
        wait = self.min_interval - (time.monotonic() - self._last_call)
        if wait > 0:
            time.sleep(wait)
        self._last_call = time.monotonic()

    def complete_json(self, system: str, user: str, temperature: float | None = None) -> dict[str, Any]:
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        name = f"{self.provider} ({self.model})"
        last_error = ""
        for attempt in range(1, self.max_retries + 1):
            payload: dict[str, Any] = {
                "model": self.model,
                "temperature": self.temperature if temperature is None else temperature,
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            }
            if self.json_mode:
                payload["response_format"] = {"type": "json_object"}

            self._throttle()
            try:
                resp = self.session.post(self.url, json=payload, headers=headers, timeout=self.timeout)
            except requests.RequestException as exc:
                last_error = f"network error: {exc}"
                time.sleep(min(2 ** attempt, 30))
                continue

            if resp.status_code == 200:
                self.calls += 1
                try:
                    return parse_json_content(resp.json()["choices"][0]["message"]["content"])
                except (KeyError, IndexError, TypeError, ValueError) as exc:
                    last_error = f"unparseable response: {exc}"
                    continue

            body = resp.text[:300]
            if resp.status_code == 400 and self.json_mode and "response_format" in body:
                self.log.info("%s does not support JSON mode; continuing without it", name)
                self.json_mode = False
                continue
            if resp.status_code == 429 or resp.status_code >= 500:
                retry_after = resp.headers.get("retry-after")
                try:
                    delay = float(retry_after) if retry_after else 2 ** attempt
                except ValueError:
                    delay = 2 ** attempt
                last_error = f"HTTP {resp.status_code}"
                self.log.debug("LLM %s, retrying in %.1fs", resp.status_code, delay)
                time.sleep(min(delay, 60))
                continue
            if resp.status_code in (401, 403, 404):
                raise LLMConfigError(f"{name} rejected the request (HTTP {resp.status_code}): {body}. "
                                     "Check the API key and llm.model in config.yaml.")
            raise LLMError(f"{name} request failed: HTTP {resp.status_code} {body}")
        raise LLMError(f"{name} request failed after {self.max_retries} attempts: {last_error}")
