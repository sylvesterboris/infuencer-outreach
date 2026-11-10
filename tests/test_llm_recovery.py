"""LLM output parsing and recovery after LLM outages / misconfiguration."""
import pytest

from src.discovery import collect, discover
from src.enrichment import run_enrichment
from src.filtering import run_filtering
from src.llm import LLMConfigError, LLMError, parse_json_content
from src.personalization import run_personalization
from tests.fakes import FakeLLM, FakeYouTube


class BrokenLLM:
    model = "broken"

    def __init__(self, exc: Exception):
        self.exc = exc
        self.calls = 0

    def complete_json(self, *args, **kwargs):
        self.calls += 1
        raise self.exc


def _collect_and_filter(cfg, storage):
    yt = FakeYouTube()
    discover(cfg, yt, storage)
    collect(cfg, yt, storage)
    run_filtering(cfg, storage)


@pytest.mark.parametrize(
    "content",
    [
        '{"a": 1}',
        '```json\n{"a": 1}\n```',
        '<think>reasoning here {not json}</think>\n{"a": 1}',
        'Sure! Here it is:\n{"a": 1}\nHope that helps.',
    ],
)
def test_parse_json_content_tolerates_wrappers(content):
    assert parse_json_content(content) == {"a": 1}


def test_parse_json_content_rejects_non_objects():
    with pytest.raises(ValueError):
        parse_json_content("[1, 2]")


def test_failed_messages_are_retried_on_next_run(cfg, storage):
    _collect_and_filter(cfg, storage)
    run_enrichment(cfg, storage, FakeLLM())
    first = run_personalization(cfg, storage, BrokenLLM(LLMError("HTTP 503")))
    assert first["generated"] == 0 and first["failed"] > 0
    assert all(m["validation_status"] == "failed" for m in storage.list_messages())

    # LLM is back: the failed ones must be regenerated without --regenerate
    second = run_personalization(cfg, storage, FakeLLM())
    assert second["generated"] == first["failed"] and second["skipped"] == 0
    assert all(m["validation_status"] == "ok" for m in storage.list_messages())


def test_config_error_stops_personalization_after_one_call(cfg, storage):
    _collect_and_filter(cfg, storage)
    run_enrichment(cfg, storage, FakeLLM())
    llm = BrokenLLM(LLMConfigError("HTTP 404 model_not_found"))
    with pytest.raises(LLMConfigError):
        run_personalization(cfg, storage, llm)
    assert llm.calls == 1


def test_enrichment_falls_back_then_upgrades_when_llm_returns(cfg, storage):
    _collect_and_filter(cfg, storage)
    broken = BrokenLLM(LLMConfigError("HTTP 404 model_not_found"))
    run_enrichment(cfg, storage, broken)
    assert broken.calls == 1  # stopped using the LLM after the first config error
    shortlisted = storage.list_influencers(filter_status="PASS")
    assert shortlisted and all(r.themes_source == "keyword-fallback" for r in shortlisted)

    res = run_enrichment(cfg, storage, FakeLLM())
    assert res["enriched"] == len(shortlisted)
    for r in storage.list_influencers(filter_status="PASS"):
        assert r.themes_source == "llm"
        assert not any(e.startswith("LLM analysis failed") for e in r.errors)

    # nothing left to redo on a third run
    assert run_enrichment(cfg, storage, FakeLLM())["enriched"] == 0
