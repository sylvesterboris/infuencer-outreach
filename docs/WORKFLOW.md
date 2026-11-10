# Automation Workflow

The whole workflow is automated in Python and driven by one CLI (`run_pipeline.py`). Every stage reads and writes the same SQLite database (`outputs/pipeline.db`), which acts as the workflow's state store. That makes each stage resumable, safe to re-run and schedulable on its own.

```mermaid
flowchart TD
    A[YouTube Data API<br/>search.list, 15 tech queries] -->|channel IDs, de-duplicated| B[(SQLite: influencers<br/>stage = discovered)]
    B --> C[Collect<br/>channels.list + playlistItems + videos.list<br/>subs, views, likes, comments, last 12 uploads]
    C -->|metrics, emails & socials from descriptions| D{Filter & classify<br/>9 rule checks}
    D -->|FAIL + reasons| X[(dataset: FAIL)]
    D -->|PASS| E[Enrich<br/>1. creator's own website, robots.txt respected<br/>2. LLM content analysis: themes, tone, sub-niche]
    E --> F[Personalize<br/>rule-based angle + LLM email & DM<br/>validated, auto-retried with feedback]
    F --> G{Human review<br/>approve / reject<br/>or --auto-approve validated}
    G -->|approved + valid email| H[Send<br/>dry-run .eml or SMTP<br/>dedupe by channel and inbox, daily cap]
    G -->|no email| I[Log SKIPPED_NO_EMAIL]
    F --> J[Instagram DM queue<br/>manual send, then dm-mark]
    H --> K[(outreach_log)]
    I --> K
    J --> K
    K --> L[Export<br/>CSV, XLSX, HTML report]
```

## Stages and commands

| # | Stage | Command | Input → output | Automated? |
|---|---|---|---|---|
| 1 | Discovery | `discover` | search queries → unique channel IDs | Yes |
| 2 | Data collection | `collect` | channel IDs → stats, recent videos, ER, public contacts | Yes |
| 3 | Filtering & classification | `filter` | metrics → PASS/FAIL + reasons, sub-niche, tier | Yes |
| 4 | Enrichment | `enrich` | shortlist → website email, themes, tone, audience | Yes |
| 5 | AI personalization | `personalize` | enriched profile → email (60-90 words) + DM (15-30 words) | Yes |
| 6 | Review | `review`, `approve`, `reject` | drafts → approved / rejected | Human step (optional: `--auto-approve`) |
| 7 | Sending | `send` | approved + valid email → SIMULATED / SENT / FAILED | Yes |
| 7b | Instagram DM | `dm-queue`, `dm-mark` | DMs → manual queue → SENT_MANUALLY | Queue automated, sending manual (platform rules) |
| 8 | Tracking | `export`, `status` | DB → tracker, log, report | Yes |

`python run_pipeline.py run --auto-approve` chains all of it in one command.

## Record lifecycle

```
discovered → collected → filtered (PASS | FAIL) → enriched → message: pending → approved → email: SIMULATED | SENT | FAILED
                                                                          ↘ rejected (never sent)
```

## Scheduling

Because every stage only picks up records that still need work, the pipeline can run on a schedule without duplicating anything, for example a daily cron:

```cron
0 9 * * *  cd /path/to/influencer-outreach && .venv/bin/python run_pipeline.py run --auto-approve >> outputs/cron.log 2>&1
```

New queries added to `config.yaml` only process the new channels, and the YouTube cache (24h) plus quota budget keep each run within the free tier.

## Safety rails built into the workflow

- **No fabricated data:** missing values are written as `Not Found` / `Not Available`.
- **Validation gate:** messages that fail validation are never auto-approved.
- **Duplicate protection:** checked by channel *and* by email address before every send.
- **Dry-run by default:** real SMTP only with `--mode smtp`. `test_recipient_override` routes real sends to your own inbox for testing.
- **Instagram:** DMs are queued for manual sending, not automated.
- **LLM failure recovery:** a bad model or key stops the stage immediately. Rate-limited or failed calls are retried on the next run.
