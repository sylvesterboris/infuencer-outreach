#!/usr/bin/env python3
"""CLI for the micro-influencer outreach pipeline.

    python run_pipeline.py run --auto-approve     # whole pipeline, dry-run sending
    python run_pipeline.py status                 # where every record is

Individual stages: discover, collect, filter, enrich, personalize, review, approve,
reject, send, dm-queue, dm-mark, export. Each stage is resumable.
"""
from __future__ import annotations

import argparse
import sys
import textwrap

from src.config import ConfigError, load_config, require_secret
from src.discovery import collect, discover
from src.dm_workflow import build_dm_queue, mark_dm
from src.enrichment import run_enrichment
from src.export import run_export
from src.filtering import run_filtering
from src.llm import DEFAULT_MODEL, DEFAULT_PROVIDER, GroqClient, LLMConfigError
from src.personalization import run_personalization
from src.sending import SendError, run_sending
from src.storage import Storage
from src.utils import setup_logging, word_count
from src.youtube_client import YouTubeClient


def make_youtube(cfg, storage) -> YouTubeClient:
    ycfg = cfg.get("youtube", {})
    return YouTubeClient(
        require_secret(cfg, "youtube_api_key", "discovery"),
        storage=storage,
        quota_budget=int(ycfg.get("daily_quota_budget", 8000)),
        cache_ttl_hours=float(ycfg.get("cache_ttl_hours", 24)),
        timeout=float(ycfg.get("request_timeout_seconds", 20)),
    )


def make_llm(cfg, required: bool = True) -> GroqClient | None:
    lcfg = cfg.get("llm", {})
    provider = lcfg.get("provider", DEFAULT_PROVIDER)
    secret_key = {"openai": "openai_api_key", "xai": "xai_api_key"}.get(provider, "groq_api_key")
    key = cfg["secrets"].get(secret_key)
    if not key:
        if required:
            require_secret(cfg, secret_key, "AI personalization")
        return None
    return GroqClient(
        key,
        model=lcfg.get("model", DEFAULT_MODEL),
        temperature=float(lcfg.get("temperature", 0.7)),
        min_interval=float(lcfg.get("min_seconds_between_calls", 1.0)),
        provider=provider,
    )


def print_status(storage: Storage) -> None:
    infs = storage.list_influencers()
    msgs = storage.list_messages()
    log = storage.outreach_log()
    by_stage: dict[str, int] = {}
    for i in infs:
        by_stage[i.stage] = by_stage.get(i.stage, 0) + 1
    passed = [i for i in infs if i.filter_status == "PASS"]
    print("\nPipeline status")
    print(f"  influencers discovered : {len(infs)}  {by_stage}")
    print(f"  passed / failed filter : {len(passed)} / {sum(i.filter_status == 'FAIL' for i in infs)}")
    print(f"  shortlist with email   : {sum(i.has_valid_email for i in passed)}")
    print(f"  messages               : {len(msgs)} "
          f"(ok {sum(m['validation_status'] == 'ok' for m in msgs)}, "
          f"needs review {sum(m['validation_status'] == 'needs_review' for m in msgs)}, "
          f"approved {sum(m['review_status'] == 'approved' for m in msgs)})")
    counts: dict[str, int] = {}
    for row in log:
        k = f"{row['channel']}:{row['status']}"
        counts[k] = counts.get(k, 0) + 1
    print(f"  outreach log entries   : {counts or 'none'}\n")


def cmd_review(storage: Storage, show_all: bool) -> None:
    for m in storage.list_messages():
        if not show_all and m["review_status"] != "pending":
            continue
        rec = storage.get_influencer(m["channel_id"])
        if rec is None:
            continue
        print("=" * 78)
        print(f"{rec.name}  [{m['channel_id']}]  validation={m['validation_status']}  review={m['review_status']}")
        print(f"to: {rec.email}   angle: {m['angle']}")
        if m["validation_notes"]:
            print(f"notes: {m['validation_notes']}")
        print(f"\nSubject: {m['email_subject']}\n")
        print(textwrap.fill(m["email_body"] or "", 78) + f"\n({word_count(m['email_body'] or '')} words)\n")
        print("DM: " + textwrap.fill(m["dm_text"] or "", 74) + f"\n({word_count(m['dm_text'] or '')} words)")
    print("=" * 78)
    print("Approve with: python run_pipeline.py approve --ids <channel_id> ...   (or --all)")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Automated micro-influencer outreach pipeline")
    parser.add_argument("--config", default=None, help="path to config.yaml")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    run_p = sub.add_parser("run", help="discover -> collect -> filter -> enrich -> personalize -> send -> export")
    run_p.add_argument("--auto-approve", action="store_true", help="approve messages that passed validation")
    run_p.add_argument("--skip-send", action="store_true")
    run_p.add_argument("--mode", choices=["dry-run", "smtp"], default=None)

    sub.add_parser("discover", help="search YouTube for candidate channels")
    p = sub.add_parser("collect", help="fetch channel + recent video stats")
    p.add_argument("--force", action="store_true", help="re-collect every channel")
    p = sub.add_parser("filter", help="apply filtering rules")
    p.add_argument("--refilter", action="store_true", help="re-evaluate everything (after changing config)")
    p = sub.add_parser("enrich", help="website email lookup + LLM content analysis for the shortlist")
    p.add_argument("--force", action="store_true")
    p = sub.add_parser("personalize", help="generate email pitch + Instagram DM")
    p.add_argument("--regenerate", action="store_true")
    p = sub.add_parser("review", help="print generated messages for review")
    p.add_argument("--all", action="store_true", help="include already reviewed messages")
    p = sub.add_parser("approve", help="approve messages for sending")
    p.add_argument("--ids", nargs="*", default=[])
    p.add_argument("--all", action="store_true", help="approve every message that passed validation")
    p.add_argument("--include-needs-review", action="store_true")
    p = sub.add_parser("reject", help="reject messages so they are never sent")
    p.add_argument("--ids", nargs="+", required=True)
    p = sub.add_parser("send", help="send or simulate approved emails")
    p.add_argument("--mode", choices=["dry-run", "smtp"], default=None)
    p.add_argument("--limit", type=int, default=None)
    sub.add_parser("dm-queue", help="build the manual Instagram DM queue")
    p = sub.add_parser("dm-mark", help="mark a DM as sent/skipped after doing it by hand")
    p.add_argument("--id", required=True, help="channel id")
    p.add_argument("--status", default="SENT_MANUALLY", choices=["SENT_MANUALLY", "SKIPPED_MANUALLY"])
    sub.add_parser("export", help="write CSV / XLSX / HTML outputs")
    sub.add_parser("status", help="summary of where records are")

    args = parser.parse_args(argv)
    try:
        cfg = load_config(args.config)
    except ConfigError as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        return 2
    log = setup_logging(cfg["paths"]["output_dir"], args.verbose)
    storage = Storage(cfg["paths"]["database"])

    try:
        c = args.command
        if c in ("run", "discover", "collect"):
            yt = make_youtube(cfg, storage)
            if c in ("run", "discover"):
                log.info("Discovery: %s", discover(cfg, yt, storage))
            if c in ("run", "collect"):
                log.info("Collection: %s", collect(cfg, yt, storage, force=getattr(args, "force", False)))
        if c in ("run", "filter"):
            run_filtering(cfg, storage, refilter=getattr(args, "refilter", False))
        if c in ("run", "enrich"):
            llm = make_llm(cfg, required=False)
            if llm is None:
                log.warning("LLM API key not set: using keyword-based content themes")
            log.info("Enrichment: %s", run_enrichment(cfg, storage, llm, force=getattr(args, "force", False)))
        if c in ("run", "personalize"):
            log.info("Personalization: %s", run_personalization(cfg, storage, make_llm(cfg),
                                                               regenerate=getattr(args, "regenerate", False)))
        if c == "run" and args.auto_approve:
            ids = [m["channel_id"] for m in storage.list_messages("pending") if m["validation_status"] == "ok"]
            log.info("Auto-approved %d validated messages (needs_review ones are left for a human)",
                     storage.set_review_status(ids, "approved"))
        if c == "run" and not args.skip_send:
            log.info("Sending: %s", run_sending(cfg, storage, mode=args.mode))
            log.info("DM queue: %s", build_dm_queue(storage))
        if c == "review":
            cmd_review(storage, args.all)
        if c == "approve":
            if args.all:
                ids = [m["channel_id"] for m in storage.list_messages("pending")
                       if m["validation_status"] == "ok" or (args.include_needs_review and m["validation_status"] == "needs_review")]
            else:
                ids = args.ids
            print(f"Approved {storage.set_review_status(ids, 'approved')} message(s)")
        if c == "reject":
            print(f"Rejected {storage.set_review_status(args.ids, 'rejected')} message(s)")
        if c == "send":
            log.info("Sending: %s", run_sending(cfg, storage, mode=args.mode, limit=args.limit))
        if c == "dm-queue":
            log.info("DM queue: %s", build_dm_queue(storage))
        if c == "dm-mark":
            print(mark_dm(storage, args.id, args.status))
        if c in ("run", "export"):
            files = run_export(cfg, storage)
            log.info("Exported:\n  " + "\n  ".join(files.values()))
        if c in ("run", "status"):
            print_status(storage)
    except (ConfigError, SendError, LLMConfigError, ValueError) as exc:
        log.error("%s", exc)
        return 1
    finally:
        storage.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
