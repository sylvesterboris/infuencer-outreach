"""End-to-end run of every stage on synthetic fixtures with fake API/LLM clients."""
import csv
from pathlib import Path

import pytest

from src.discovery import collect, discover
from src.dm_workflow import build_dm_queue, mark_dm
from src.enrichment import run_enrichment
from src.export import run_export
from src.filtering import run_filtering
from src.models import NOT_FOUND
from src.personalization import choose_angle, run_personalization, validate_messages
from src.sending import run_sending
from tests.fakes import FakeLLM, FakeYouTube


def _run_until_messages(cfg, storage, llm=None):
    yt = FakeYouTube()
    discover(cfg, yt, storage)
    collect(cfg, yt, storage)
    run_filtering(cfg, storage)
    run_enrichment(cfg, storage, llm or FakeLLM())
    run_personalization(cfg, storage, llm or FakeLLM())
    return yt


def test_discovery_dedupes_channels_across_queries(cfg, storage):
    yt = FakeYouTube()
    cfg["discovery"]["queries"] = ["tech review", "coding tutorial"]
    cfg["discovery"]["target_candidates"] = 1000
    res = discover(cfg, yt, storage)
    assert res["new_channels"] == 9
    rec = storage.get_influencer("CHA")
    assert rec.discovered_via == ["tech review", "coding tutorial"]


def test_filtering_reasons(cfg, storage):
    yt = FakeYouTube()
    discover(cfg, yt, storage)
    collect(cfg, yt, storage)
    run_filtering(cfg, storage)
    recs = {r.channel_id: r for r in storage.list_influencers()}

    assert recs["CHA"].filter_status == "PASS"
    assert recs["CHE"].filter_status == "PASS"
    assert recs["CHI"].filter_status == "PASS"
    expected_fail = {
        "CHB": "Subscriber count",
        "CHC": "Subscriber count",
        "CHD": "Niche relevance",
        "CHF": "Recent activity",
        "CHG": "Engagement rate",
        "CHH": "Brand safety",
    }
    for cid, criterion in expected_fail.items():
        assert recs[cid].filter_status == "FAIL", cid
        assert any(reason.startswith(criterion) for reason in recs[cid].fail_reasons), (cid, recs[cid].fail_reasons)

    # demographics are recorded as not evaluable, never failed or guessed
    demo = [c for c in recs["CHA"].filter_checks if c["criterion"] == "Audience demographics"][0]
    assert demo["passed"] is None and demo["value"] == "Not Available"


def test_contacts_extracted_and_missing_marked(cfg, storage):
    _run_until_messages(cfg, storage)
    a, e, f = (storage.get_influencer(x) for x in ("CHA", "CHE", "CHF"))
    assert a.email == "collab@ankit.example.test" and a.email_source == "channel description"
    assert a.instagram_url == "https://www.instagram.com/test.ankit/"
    assert e.email == NOT_FOUND
    assert e.website == "https://riya.example.test"
    assert f.email == "gadgets@sleepy.example.test"  # decoded from [at]/[dot]


def test_messages_generated_and_valid(cfg, storage):
    _run_until_messages(cfg, storage)
    msgs = {m["channel_id"]: m for m in storage.list_messages()}
    assert set(msgs) == {"CHA", "CHE", "CHI"}
    for m in msgs.values():
        assert m["validation_status"] == "ok", m["validation_notes"]
    assert msgs["CHI"]["angle"].startswith("UGC content creation")  # shorts-heavy channel


def test_retry_with_feedback_fixes_bad_draft(cfg, storage):
    llm = FakeLLM(bad_first=True)
    _run_until_messages(cfg, storage, llm)
    first = storage.list_messages()[0]
    assert first["attempts"] == 2 and first["validation_status"] == "ok"


def test_validator_catches_problems(cfg, storage):
    _run_until_messages(cfg, storage)
    rec = storage.get_influencer("CHA")
    bad = {
        "email_subject": "Quick idea",
        "email_body": "Hi [Name], we have 2 million users. " * 3,
        "dm": "Hi #collab",
    }
    issues = " ".join(validate_messages(bad, rec, "{}", cfg["messages"]))
    for expected in ("placeholder", "words", "address the creator", "number", "hashtags"):
        assert expected in issues


def test_angle_rules(cfg, storage):
    _run_until_messages(cfg, storage)
    che = storage.get_influencer("CHE")  # 8K subs, 4.5% ER
    assert choose_angle(che)["type"] == "Brand ambassador program"
    che.engagement_rate = 0.02
    assert choose_angle(che)["type"] == "Affiliate campaign"
    cha = storage.get_influencer("CHA")  # 25K subs, educational titles
    cha.engagement_rate = 0.02
    assert choose_angle(cha)["type"] == "Sponsorship"


def test_sending_requires_approval_then_dedupes(cfg, storage):
    _run_until_messages(cfg, storage)

    res = run_sending(cfg, storage, mode="dry-run")
    assert res["simulated"] == 0 and res["skipped_not_approved"] == 2 and res["skipped_no_email"] == 1

    storage.set_review_status([m["channel_id"] for m in storage.list_messages()], "approved")
    res = run_sending(cfg, storage, mode="dry-run")
    # CHA sent; CHI shares CHA's inbox -> duplicate; CHE has no email
    assert res["simulated"] == 1 and res["skipped_duplicate"] == 1
    eml = Path(cfg["paths"]["output_dir"]) / "outbox" / "CHA.eml"
    content = eml.read_text()
    assert "To: collab@ankit.example.test" in content and "List-Unsubscribe" in content

    res = run_sending(cfg, storage, mode="dry-run")
    assert res["simulated"] == 0 and res["skipped_duplicate"] == 2
    # no-email skip is logged once, not on every run
    skips = [r for r in storage.outreach_log("email") if r["status"] == "SKIPPED_NO_EMAIL"]
    assert len(skips) == 1


class _FakeSMTP:
    mode = "smtp"

    def __init__(self, fail=False):
        self.sent, self.fail = [], fail

    def send(self, em, channel_id):
        if self.fail:
            raise OSError("connection refused")
        self.sent.append(em["To"])
        return "<id@test>"


def test_dry_run_does_not_block_real_send_and_override(cfg, storage):
    _run_until_messages(cfg, storage)
    storage.set_review_status([m["channel_id"] for m in storage.list_messages()], "approved")
    run_sending(cfg, storage, mode="dry-run")

    cfg["sending"]["test_recipient_override"] = "me@inbox.example.test"
    smtp = _FakeSMTP()
    res = run_sending(cfg, storage, mode="smtp", sender_impl=smtp)
    assert res["sent"] == 1 and smtp.sent == ["me@inbox.example.test"]
    log = storage.latest_outreach("CHA", "email")
    assert log["status"] == "SENT_TEST" and log["recipient"] == "collab@ankit.example.test"

    cfg["sending"]["test_recipient_override"] = ""
    smtp = _FakeSMTP()
    res = run_sending(cfg, storage, mode="smtp", sender_impl=smtp)
    assert smtp.sent == ["collab@ankit.example.test"]
    res = run_sending(cfg, storage, mode="smtp", sender_impl=_FakeSMTP())
    assert res["sent"] == 0 and res["skipped_duplicate"] == 2


def test_failed_send_is_logged_and_retryable(cfg, storage):
    _run_until_messages(cfg, storage)
    storage.set_review_status([m["channel_id"] for m in storage.list_messages()], "approved")
    res = run_sending(cfg, storage, mode="smtp", sender_impl=_FakeSMTP(fail=True))
    assert res["failed"] >= 1
    assert storage.latest_outreach("CHA", "email")["status"] == "FAILED"
    ok = _FakeSMTP()
    run_sending(cfg, storage, mode="smtp", sender_impl=ok)
    assert "collab@ankit.example.test" in ok.sent


def test_dm_queue_and_manual_marking(cfg, storage):
    _run_until_messages(cfg, storage)
    res = build_dm_queue(storage)
    assert res["queued"] == 3 and res["without_instagram_link"] == 2
    assert build_dm_queue(storage)["queued"] == 0  # idempotent
    assert "marked" in mark_dm(storage, "CHA")
    assert "not logging a duplicate" in mark_dm(storage, "CHA")
    with pytest.raises(ValueError):
        mark_dm(storage, "NOPE")


def test_export_files(cfg, storage):
    _run_until_messages(cfg, storage)
    files = run_export(cfg, storage)
    for p in files.values():
        assert Path(p).exists() and Path(p).stat().st_size > 0
    with open(files["dataset_csv"], encoding="utf-8-sig") as fh:
        rows = list(csv.DictReader(fh))
    assert len(rows) == 9
    hidden = [r for r in rows if r["Channel ID"] == "CHC"][0]
    assert hidden["Followers"] == "Hidden by creator" and hidden["Status"] == "FAIL"
    no_likes = [r for r in rows if r["Channel ID"] == "CHG"][0]
    assert no_likes["Engagement Rate"] == "Not Available"
    assert "Content Themes" in rows[0] and all(r["Content Themes"] for r in rows)


def test_compose_email_headers_with_display_name_sender():
    from src.models import Influencer
    from src.sending import compose_email
    rec = Influencer(channel_id="UCX", name="Test Channel")
    msg = {"email_subject": "Hello", "email_body": "Body text"}
    em = compose_email(rec, msg, {"name": "B"}, "Aman <aman@sender.example.test>", "to@example.test")
    assert em["List-Unsubscribe"] == "<mailto:aman@sender.example.test?subject=unsubscribe>"
    assert em["Message-ID"].endswith("@sender.example.test>")
