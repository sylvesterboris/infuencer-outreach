"""Stage 8: export the dataset, messages and tracker to CSV, XLSX and an HTML report."""
from __future__ import annotations

import csv
import html
from pathlib import Path
from typing import Any

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from .dm_workflow import DM_CHANNEL, dm_rows
from .enrichment import keyword_fallback
from .models import NOT_AVAILABLE, NOT_FOUND, Influencer
from .storage import Storage
from .utils import now_iso, word_count


def _fmt_int(v: int | float | None, hidden: bool = False) -> str | int:
    if hidden:
        return "Hidden by creator"
    return int(v) if v is not None else NOT_AVAILABLE


def _fmt_pct(v: float | None) -> str:
    return f"{v:.2%}" if v is not None else NOT_AVAILABLE


def _themes(rec: Influencer) -> tuple[str, str]:
    if rec.content_themes:
        return ", ".join(rec.content_themes), rec.themes_source or ""
    if rec.recent_videos:
        return ", ".join(keyword_fallback(rec)["content_themes"]), "keyword-fallback"
    return NOT_AVAILABLE, ""


def dataset_rows(storage: Storage) -> list[dict[str, Any]]:
    rows = []
    for rec in storage.list_influencers():
        themes, themes_src = _themes(rec)
        status = rec.filter_status if rec.filter_status in ("PASS", "FAIL") else f"PENDING ({rec.stage})"
        rows.append(
            {
                "Name": rec.name,
                "Platform": rec.platform,
                "Followers": _fmt_int(rec.subscribers, rec.subscribers_hidden),
                "Engagement Rate": _fmt_pct(rec.engagement_rate),
                "Niche": rec.category or "Technology (candidate)",
                "Sub-niche": rec.sub_niche or NOT_AVAILABLE,
                "Email": rec.email or NOT_FOUND,
                "Email Source": rec.email_source or "",
                "Profile URL": rec.profile_url,
                "Content Themes": themes,
                "Themes Source": themes_src,
                "Content Tone": rec.content_tone or "",
                "Status": status,
                "Fail Reasons": " | ".join(rec.fail_reasons),
                "Tier": rec.tier or "",
                "Relevance Score": rec.relevance_score if rec.relevance_score is not None else "",
                "Educational Ratio": rec.educational_ratio if rec.educational_ratio is not None else "",
                "Median Views (recent)": _fmt_int(rec.median_views),
                "Videos Used for ER": rec.videos_analysed,
                "Days Since Upload": rec.days_since_upload if rec.days_since_upload is not None else "",
                "Creator Country": rec.country or "Not set",
                "Instagram": rec.instagram_url or NOT_FOUND,
                "TikTok": rec.tiktok_url or NOT_FOUND,
                "X / Twitter": rec.twitter_url or NOT_FOUND,
                "LinkedIn": rec.linkedin_url or NOT_FOUND,
                "Website": rec.website or NOT_FOUND,
                "Audience Age": NOT_AVAILABLE,
                "Audience Gender": NOT_AVAILABLE,
                "Audience Geography": NOT_AVAILABLE,
                "Seems Made For (inferred from content)": rec.inferred_audience or "",
                "Discovered Via": "; ".join(rec.discovered_via),
                "Channel ID": rec.channel_id,
                "Errors": " | ".join(rec.errors),
            }
        )
    return rows


def message_rows(storage: Storage) -> list[dict[str, Any]]:
    rows = []
    for msg in storage.list_messages():
        rec = storage.get_influencer(msg["channel_id"])
        if rec is None:
            continue
        rows.append(
            {
                "Influencer": rec.name,
                "Email": rec.email,
                "Profile URL": rec.profile_url,
                "Collaboration Angle": msg.get("angle") or "",
                "Email Subject": msg.get("email_subject") or "",
                "Email Pitch": msg.get("email_body") or "",
                "Email Words": word_count(msg.get("email_body") or ""),
                "Instagram DM": msg.get("dm_text") or "",
                "DM Words": word_count(msg.get("dm_text") or ""),
                "Validation": msg.get("validation_status") or "",
                "Validation Notes": msg.get("validation_notes") or "",
                "Review Status": msg.get("review_status") or "",
                "Model": msg.get("model") or "",
                "Attempts": msg.get("attempts") or 0,
                "Generated At": msg.get("generated_at") or "",
                "Channel ID": rec.channel_id,
            }
        )
    return rows


def tracker_rows(storage: Storage) -> list[dict[str, Any]]:
    rows = []
    for rec in storage.list_influencers(filter_status="PASS"):
        msg = storage.get_message(rec.channel_id)
        last = storage.latest_outreach(rec.channel_id, "email")
        dm_last = storage.latest_outreach(rec.channel_id, DM_CHANNEL)
        if last:
            status = last["status"]
        elif not msg or not msg.get("email_body"):
            status = "NO_MESSAGE"
        elif not rec.has_valid_email:
            status = "NO_EMAIL"
        elif msg.get("review_status") != "approved":
            status = "AWAITING_APPROVAL"
        else:
            status = "READY"
        sent = {"SENT": "Yes", "SENT_TEST": "Yes (test inbox)", "SIMULATED": "Simulated"}.get(status, "No")
        rows.append(
            {
                "Influencer": rec.name,
                "Email": rec.email,
                "Message Generated": "Yes" if msg and msg.get("email_body") else "No",
                "Review": (msg or {}).get("review_status", ""),
                "Sent": sent,
                "Date": (last or {}).get("created_at", ""),
                "Status": status,
                "Mode": (last or {}).get("mode", ""),
                "Delivered To": (last or {}).get("delivered_to", "") or "",
                "Error": (last or {}).get("error", "") or "",
                "Instagram DM Status": dm_last["status"] if dm_last else "NOT_QUEUED",
                "Channel ID": rec.channel_id,
            }
        )
    return rows


def log_rows(storage: Storage) -> list[dict[str, Any]]:
    return [
        {
            "Time (UTC)": r["created_at"],
            "Influencer": r["influencer_name"],
            "Channel": r["channel"],
            "Status": r["status"],
            "Mode": r["mode"] or "",
            "Recipient": r["recipient"] or "",
            "Delivered To": r["delivered_to"] or "",
            "Reference": r["message_ref"] or "",
            "Error / Note": r["error"] or "",
            "Channel ID": r["channel_id"],
        }
        for r in storage.outreach_log()
    ]


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    with open(path, "w", newline="", encoding="utf-8-sig") as fh:
        if not rows:
            fh.write("")
            return
        writer = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def write_xlsx(path: Path, sheets: dict[str, list[dict[str, Any]]]) -> None:
    wb = Workbook()
    wb.remove(wb.active)
    header_fill = PatternFill("solid", fgColor="1F2937")
    pass_fill = PatternFill("solid", fgColor="DCFCE7")
    fail_fill = PatternFill("solid", fgColor="FEE2E2")
    for name, rows in sheets.items():
        ws = wb.create_sheet(title=name[:31])
        if not rows:
            ws.append(["No rows yet"])
            continue
        headers = list(rows[0].keys())
        ws.append(headers)
        for cell in ws[1]:
            cell.font = Font(bold=True, color="FFFFFF")
            cell.fill = header_fill
            cell.alignment = Alignment(vertical="center", wrap_text=True)
        for row in rows:
            ws.append([row.get(h, "") for h in headers])
        ws.freeze_panes = "B2"
        ws.auto_filter.ref = ws.dimensions
        status_col = headers.index("Status") + 1 if "Status" in headers else None
        for r in range(2, ws.max_row + 1):
            if status_col:
                val = str(ws.cell(r, status_col).value)
                fill = pass_fill if val in ("PASS", "SENT", "SIMULATED", "SENT_TEST") else fail_fill if val in ("FAIL", "FAILED") else None
                if fill:
                    ws.cell(r, status_col).fill = fill
            for c in range(1, len(headers) + 1):
                ws.cell(r, c).alignment = Alignment(vertical="top", wrap_text=True)
        for i, h in enumerate(headers, 1):
            longest = max([len(str(h))] + [len(str(row.get(h, ""))) for row in rows[:200]])
            ws.column_dimensions[get_column_letter(i)].width = max(10, min(longest + 2, 60))
    wb.save(path)


# --------------------------------------------------------------------------- HTML report
def _table(rows: list[dict[str, Any]], cols: list[str], status_col: str | None = "Status") -> str:
    if not rows:
        return "<p class='empty'>Nothing here yet.</p>"
    head = "".join(f"<th>{html.escape(c)}</th>" for c in cols)
    body = []
    for row in rows:
        cells = []
        for c in cols:
            val = str(row.get(c, ""))
            if c == status_col:
                cls = "ok" if val in ("PASS", "SENT", "SIMULATED", "SENT_TEST", "SENT_MANUALLY") else "bad" if val in ("FAIL", "FAILED") else "mid"
                cells.append(f"<td><span class='pill {cls}'>{html.escape(val)}</span></td>")
            elif c in ("Profile URL", "Instagram") and val.startswith("http"):
                cells.append(f"<td><a href='{html.escape(val)}' target='_blank' rel='noopener'>open</a></td>")
            else:
                cells.append(f"<td>{html.escape(val)}</td>")
        body.append("<tr>" + "".join(cells) + "</tr>")
    return f"<div class='scroll'><table><thead><tr>{head}</tr></thead><tbody>{''.join(body)}</tbody></table></div>"


def write_html_report(path: Path, storage: Storage, data: dict[str, list[dict[str, Any]]]) -> None:
    ds, msgs, tracker, dms = data["dataset"], data["messages"], data["tracker"], data["dms"]
    passed = [r for r in ds if r["Status"] == "PASS"]
    stats = [
        ("Discovered", len(ds)),
        ("Passed filter", len(passed)),
        ("Failed filter", sum(r["Status"] == "FAIL" for r in ds)),
        ("Shortlist with email", sum(r["Email"] != NOT_FOUND for r in passed)),
        ("Messages generated", len(msgs)),
        ("Emails sent / simulated", sum(r["Sent"] != "No" for r in tracker)),
        ("DMs queued", sum(r["Status"] != "NOT_QUEUED" for r in dms)),
    ]
    stat_html = "".join(f"<div class='stat'><b>{v}</b><span>{html.escape(k)}</span></div>" for k, v in stats)

    cards = []
    for m in msgs:
        cards.append(
            f"""<article class='card'>
  <header><h3>{html.escape(m['Influencer'])}</h3><span class='pill {'ok' if m['Validation']=='ok' else 'mid'}'>{html.escape(m['Validation'])}</span></header>
  <p class='meta'>{html.escape(m['Collaboration Angle'])} · to {html.escape(m['Email'])}</p>
  <p class='label'>Email · {m['Email Words']} words</p>
  <p class='subject'>{html.escape(m['Email Subject'])}</p>
  <p>{html.escape(m['Email Pitch'])}</p>
  <p class='label'>Instagram DM · {m['DM Words']} words</p>
  <p class='dm'>{html.escape(m['Instagram DM'])}</p>
  {f"<p class='note'>{html.escape(m['Validation Notes'])}</p>" if m['Validation Notes'] else ''}
</article>"""
        )

    doc = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Outreach Pipeline Report</title>
<style>
:root {{ --bg:#f7f7f5; --fg:#1c1c1a; --muted:#6b6b66; --card:#fff; --line:#e4e4df; --ok:#dcfce7; --okfg:#14532d; --bad:#fee2e2; --badfg:#7f1d1d; --mid:#fef3c7; --midfg:#713f12; --accent:#2f5d50; }}
@media (prefers-color-scheme: dark) {{ :root {{ --bg:#161614; --fg:#ecece8; --muted:#a1a19a; --card:#1f1f1c; --line:#33332f; --ok:#163a24; --okfg:#bbf7d0; --bad:#4a1c1c; --badfg:#fecaca; --mid:#433411; --midfg:#fde68a; --accent:#8fc1b1; }} }}
* {{ box-sizing:border-box; }}
body {{ margin:0; background:var(--bg); color:var(--fg); font:15px/1.55 system-ui,-apple-system,Segoe UI,sans-serif; }}
main {{ max-width:1200px; margin:0 auto; padding:32px 16px 64px; }}
h1 {{ font-size:26px; margin:0 0 4px; }} h2 {{ font-size:19px; margin:40px 0 12px; }}
.sub {{ color:var(--muted); margin:0 0 24px; }}
.stats {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(140px,1fr)); gap:10px; }}
.stat {{ background:var(--card); border:1px solid var(--line); border-radius:10px; padding:14px; }}
.stat b {{ display:block; font-size:26px; color:var(--accent); }} .stat span {{ color:var(--muted); font-size:13px; }}
.scroll {{ overflow-x:auto; border:1px solid var(--line); border-radius:10px; background:var(--card); }}
table {{ border-collapse:collapse; width:100%; font-size:13px; }}
th, td {{ text-align:left; padding:8px 10px; border-bottom:1px solid var(--line); vertical-align:top; }}
th {{ position:sticky; top:0; background:var(--card); font-weight:600; white-space:nowrap; }}
a {{ color:var(--accent); }}
.pill {{ display:inline-block; padding:1px 8px; border-radius:99px; font-size:12px; font-weight:600; white-space:nowrap; }}
.ok {{ background:var(--ok); color:var(--okfg); }} .bad {{ background:var(--bad); color:var(--badfg); }} .mid {{ background:var(--mid); color:var(--midfg); }}
.cards {{ display:grid; grid-template-columns:repeat(auto-fill,minmax(340px,1fr)); gap:14px; }}
.card {{ background:var(--card); border:1px solid var(--line); border-radius:12px; padding:16px; }}
.card header {{ display:flex; justify-content:space-between; gap:8px; align-items:center; }}
.card h3 {{ margin:0; font-size:16px; }} .card p {{ margin:6px 0; }}
.meta, .note {{ color:var(--muted); font-size:13px; }} .label {{ font-size:11px; text-transform:uppercase; letter-spacing:.06em; color:var(--muted); margin-top:12px !important; }}
.subject {{ font-weight:600; }} .dm {{ background:var(--bg); border-radius:8px; padding:8px 10px; }}
.empty {{ color:var(--muted); }}
@media (max-width:420px) {{ .cards {{ grid-template-columns:1fr; }} }}
</style></head><body><main>
<h1>Micro-Influencer Outreach Report</h1>
<p class="sub">Technology niche · YouTube · generated {html.escape(now_iso())} UTC. All metrics come from the YouTube Data API; anything unavailable is marked as such.</p>
<div class="stats">{stat_html}</div>
<h2>Shortlist (passed filtering)</h2>
{_table(passed, ["Name", "Followers", "Engagement Rate", "Sub-niche", "Email", "Content Themes", "Content Tone", "Profile URL", "Status"])}
<h2>Personalized messages</h2>
<div class="cards">{''.join(cards) or "<p class='empty'>No messages generated yet.</p>"}</div>
<h2>Outreach tracker</h2>
{_table(tracker, ["Influencer", "Email", "Message Generated", "Review", "Sent", "Date", "Status", "Mode", "Instagram DM Status"])}
<h2>Instagram DM queue (manual sending)</h2>
{_table(dms, ["Influencer", "Instagram", "DM Text", "DM Words", "Status"])}
<h2>Full dataset with pass / fail reasons</h2>
{_table(ds, ["Name", "Followers", "Engagement Rate", "Sub-niche", "Email", "Status", "Fail Reasons", "Profile URL"])}
</main></body></html>"""
    path.write_text(doc, encoding="utf-8")


def run_export(cfg: dict[str, Any], storage: Storage) -> dict[str, str]:
    out = Path(cfg["paths"]["output_dir"])
    out.mkdir(parents=True, exist_ok=True)
    data = {
        "dataset": dataset_rows(storage),
        "messages": message_rows(storage),
        "tracker": tracker_rows(storage),
        "dms": dm_rows(storage),
        "log": log_rows(storage),
    }
    shortlist = [r for r in data["dataset"] if r["Status"] == "PASS"]
    files = {
        "dataset_csv": out / "influencers_dataset.csv",
        "shortlist_csv": out / "shortlist.csv",
        "messages_csv": out / "personalized_messages.csv",
        "tracker_csv": out / "outreach_tracker.csv",
        "dm_queue_csv": out / "instagram_dm_queue.csv",
        "log_csv": out / "outreach_log.csv",
        "xlsx": out / "outreach_report.xlsx",
        "html": out / "report.html",
    }
    write_csv(files["dataset_csv"], data["dataset"])
    write_csv(files["shortlist_csv"], shortlist)
    write_csv(files["messages_csv"], data["messages"])
    write_csv(files["tracker_csv"], data["tracker"])
    write_csv(files["dm_queue_csv"], data["dms"])
    write_csv(files["log_csv"], data["log"])
    write_xlsx(
        files["xlsx"],
        {
            "Dataset": data["dataset"],
            "Shortlist": shortlist,
            "Messages": data["messages"],
            "Outreach Tracker": data["tracker"],
            "DM Queue": data["dms"],
            "Outreach Log": data["log"],
        },
    )
    write_html_report(files["html"], storage, data)
    return {k: str(v) for k, v in files.items()}
