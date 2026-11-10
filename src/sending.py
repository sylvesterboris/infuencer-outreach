"""Stage 7: sending layer + outreach log.

Modes
-----
dry-run : builds the real RFC-5322 email and writes it to outputs/outbox/*.eml, logged as SIMULATED.
smtp    : sends via SMTP (e.g. Gmail with an App Password), logged as SENT / FAILED.
          With sending.test_recipient_override set, mail goes to that address instead and is
          logged as SENT_TEST (safe end-to-end test).

Duplicate prevention
--------------------
Before every send the log is checked for the same channel *or* the same email address
(two channels run by one agency share an inbox). A SENT entry blocks all future sends;
SIMULATED / SENT_TEST only block repeats in that same mode, so a dry run never prevents
the real send later.
"""
from __future__ import annotations

import smtplib
import ssl
import time
from email.message import EmailMessage
from email.utils import make_msgid, parseaddr
from pathlib import Path
from typing import Any

from .models import Influencer
from .storage import Storage
from .utils import get_logger

BLOCKING_STATUSES = {
    "dry-run": ["SENT", "SIMULATED"],
    "smtp": ["SENT"],
    "smtp-test": ["SENT", "SENT_TEST"],
}
COUNTED_STATUSES = ["SENT", "SENT_TEST"]


class SendError(Exception):
    pass


def compose_email(
    rec: Influencer, message: dict[str, Any], brand: dict[str, Any], sender: str, to_addr: str
) -> EmailMessage:
    signature = f"\n\n{brand.get('sender_name', '')}\n{brand.get('sender_title', '')}, {brand.get('name', '')}\n{brand.get('website', '')}"
    body = message["email_body"].strip() + signature.rstrip()
    if brand.get("opt_out_line"):
        body += f"\n\n{brand['opt_out_line']}"
    em = EmailMessage()
    em["From"] = sender
    em["To"] = to_addr
    em["Subject"] = message["email_subject"]
    sender_email = parseaddr(sender)[1]  # "Aman <a@b.com>" -> "a@b.com"
    em["Message-ID"] = make_msgid(domain=(sender_email.split("@")[-1] if "@" in sender_email else "localhost"))
    if "@" in sender_email:
        em["List-Unsubscribe"] = f"<mailto:{sender_email}?subject=unsubscribe>"
    em["X-Outreach-Channel-Id"] = rec.channel_id
    em.set_content(body)
    return em


class DryRunSender:
    mode = "dry-run"

    def __init__(self, outbox_dir: str):
        self.outbox = Path(outbox_dir)
        self.outbox.mkdir(parents=True, exist_ok=True)

    def send(self, em: EmailMessage, channel_id: str) -> str:
        path = self.outbox / f"{channel_id}.eml"
        path.write_bytes(bytes(em))
        return str(path)


class SMTPSender:
    mode = "smtp"

    def __init__(self, host: str, port: int, user: str, password: str):
        if not (user and password):
            raise SendError("SMTP_USER and SMTP_PASSWORD must be set for smtp mode")
        self.host, self.port, self.user, self.password = host, port, user, password
        self._server: smtplib.SMTP | None = None

    def _connect(self) -> smtplib.SMTP:
        if self._server is None:
            ctx = ssl.create_default_context()
            if self.port == 465:
                server: smtplib.SMTP = smtplib.SMTP_SSL(self.host, self.port, context=ctx, timeout=30)
            else:
                server = smtplib.SMTP(self.host, self.port, timeout=30)
                server.starttls(context=ctx)
            server.login(self.user, self.password)
            self._server = server
        return self._server

    def send(self, em: EmailMessage, channel_id: str) -> str:
        try:
            self._connect().send_message(em)
        except smtplib.SMTPServerDisconnected:
            self._server = None
            self._connect().send_message(em)
        return em["Message-ID"]

    def close(self) -> None:
        if self._server is not None:
            try:
                self._server.quit()
            except smtplib.SMTPException:
                pass
            self._server = None


def run_sending(
    cfg: dict[str, Any],
    storage: Storage,
    mode: str | None = None,
    limit: int | None = None,
    sender_impl: Any = None,
) -> dict[str, int]:
    log = get_logger()
    scfg = cfg.get("sending", {})
    brand = cfg["brand"]
    secrets = cfg.get("secrets", {})
    mode = mode or scfg.get("mode", "dry-run")
    override = (scfg.get("test_recipient_override") or "").strip()
    effective_mode = "smtp-test" if mode == "smtp" and override else mode
    if effective_mode not in BLOCKING_STATUSES:
        raise SendError(f"Unknown sending mode '{mode}' (use dry-run or smtp)")

    if sender_impl is None:
        if mode == "dry-run":
            sender_impl = DryRunSender(str(Path(cfg["paths"]["output_dir"]) / "outbox"))
        else:
            sender_impl = SMTPSender(secrets["smtp_host"], secrets["smtp_port"], secrets["smtp_user"], secrets["smtp_password"])
    sender_addr = secrets.get("smtp_from") or f"{brand.get('sender_name', 'outreach')} <outreach@example.invalid>"

    statuses = {"sent": 0, "simulated": 0, "skipped_duplicate": 0, "skipped_no_email": 0,
                "skipped_not_approved": 0, "failed": 0}
    cap = int(scfg.get("daily_cap", 40))
    sent_today = storage.sends_today(COUNTED_STATUSES) if mode == "smtp" else 0
    delay = float(scfg.get("delay_seconds", 3)) if mode == "smtp" else 0
    require_approval = bool(scfg.get("require_approval", True))

    processed = 0
    try:
        for rec in storage.list_influencers(filter_status="PASS"):
            msg = storage.get_message(rec.channel_id)
            if not msg or not msg.get("email_body"):
                continue
            if limit is not None and processed >= limit:
                break

            # 1. valid contact email
            if not rec.has_valid_email:
                last = storage.latest_outreach(rec.channel_id, "email")
                if not last or last["status"] != "SKIPPED_NO_EMAIL":
                    storage.log_outreach(rec.channel_id, rec.name, "email", "SKIPPED_NO_EMAIL",
                                         recipient=None, mode=effective_mode, error="Contact email Not Found")
                statuses["skipped_no_email"] += 1
                continue

            # 2. reviewed + approved
            if require_approval and msg.get("review_status") != "approved":
                statuses["skipped_not_approved"] += 1
                continue

            # 3. duplicate prevention (same channel or same inbox)
            prior = storage.already_contacted("email", BLOCKING_STATUSES[effective_mode], rec.channel_id, rec.email)
            if prior:
                statuses["skipped_duplicate"] += 1
                log.info("  skip duplicate: %s (already %s on %s)", rec.name, prior["status"], prior["created_at"][:10])
                continue

            if mode == "smtp" and sent_today >= cap:
                log.warning("Daily cap of %d emails reached; stopping.", cap)
                break

            to_addr = override if effective_mode == "smtp-test" else rec.email
            em = compose_email(rec, msg, brand, sender_addr, to_addr)
            processed += 1
            try:
                ref = sender_impl.send(em, rec.channel_id)
            except Exception as exc:  # SMTP errors, auth errors, IO errors
                storage.log_outreach(rec.channel_id, rec.name, "email", "FAILED", recipient=rec.email,
                                     delivered_to=to_addr, mode=effective_mode, error=str(exc)[:500])
                statuses["failed"] += 1
                log.error("  FAILED %s: %s", rec.name, exc)
                continue

            status = {"dry-run": "SIMULATED", "smtp": "SENT", "smtp-test": "SENT_TEST"}[effective_mode]
            storage.log_outreach(rec.channel_id, rec.name, "email", status, recipient=rec.email,
                                 delivered_to=to_addr, mode=effective_mode, message_ref=ref)
            statuses["simulated" if status == "SIMULATED" else "sent"] += 1
            sent_today += status != "SIMULATED"
            log.info("  %s -> %s (%s)", status, rec.name, to_addr)
            if delay:
                time.sleep(delay)
    finally:
        if hasattr(sender_impl, "close"):
            sender_impl.close()
    return statuses
