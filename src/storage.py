"""SQLite persistence: influencers, generated messages, outreach log and API cache.

Every stage reads from and writes to this database, so the pipeline is resumable:
re-running a stage only processes records that still need it.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import timedelta
from pathlib import Path
from typing import Any, Iterable

from .models import Influencer
from .utils import now_iso, parse_iso, utc_now

SCHEMA = """
CREATE TABLE IF NOT EXISTS influencers (
    channel_id    TEXT PRIMARY KEY,
    platform      TEXT NOT NULL,
    name          TEXT,
    stage         TEXT,
    filter_status TEXT,
    data          TEXT NOT NULL,
    updated_at    TEXT
);
CREATE INDEX IF NOT EXISTS idx_inf_stage ON influencers(stage);
CREATE INDEX IF NOT EXISTS idx_inf_status ON influencers(filter_status);

CREATE TABLE IF NOT EXISTS messages (
    channel_id        TEXT PRIMARY KEY,
    email_subject     TEXT,
    email_body        TEXT,
    dm_text           TEXT,
    angle             TEXT,
    model             TEXT,
    prompt_version    TEXT,
    attempts          INTEGER,
    validation_status TEXT,
    validation_notes  TEXT,
    review_status     TEXT DEFAULT 'pending',
    generated_at      TEXT,
    reviewed_at       TEXT
);

CREATE TABLE IF NOT EXISTS outreach_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id      TEXT NOT NULL,
    influencer_name TEXT,
    recipient       TEXT,
    delivered_to    TEXT,
    channel         TEXT NOT NULL,
    status          TEXT NOT NULL,
    mode            TEXT,
    message_ref     TEXT,
    error           TEXT,
    created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_log_channel ON outreach_log(channel_id, channel);
CREATE INDEX IF NOT EXISTS idx_log_recipient ON outreach_log(recipient);

CREATE TABLE IF NOT EXISTS api_cache (
    cache_key  TEXT PRIMARY KEY,
    response   TEXT NOT NULL,
    fetched_at TEXT NOT NULL
);
"""


class Storage:
    def __init__(self, db_path: str):
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    # ------------------------------------------------------------------ influencers
    def upsert_influencer(self, inf: Influencer) -> None:
        self.conn.execute(
            """INSERT INTO influencers (channel_id, platform, name, stage, filter_status, data, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(channel_id) DO UPDATE SET
                 platform=excluded.platform, name=excluded.name, stage=excluded.stage,
                 filter_status=excluded.filter_status, data=excluded.data, updated_at=excluded.updated_at""",
            (
                inf.channel_id,
                inf.platform,
                inf.name,
                inf.stage,
                inf.filter_status,
                json.dumps(inf.to_dict(), ensure_ascii=False),
                now_iso(),
            ),
        )
        self.conn.commit()

    def get_influencer(self, channel_id: str) -> Influencer | None:
        row = self.conn.execute("SELECT data FROM influencers WHERE channel_id=?", (channel_id,)).fetchone()
        return Influencer.from_dict(json.loads(row["data"])) if row else None

    def list_influencers(
        self, stages: Iterable[str] | None = None, filter_status: str | None = None
    ) -> list[Influencer]:
        sql = "SELECT data FROM influencers WHERE 1=1"
        params: list[Any] = []
        if stages:
            stages = list(stages)
            sql += f" AND stage IN ({','.join('?' * len(stages))})"
            params.extend(stages)
        if filter_status:
            sql += " AND filter_status=?"
            params.append(filter_status)
        sql += " ORDER BY rowid"
        return [Influencer.from_dict(json.loads(r["data"])) for r in self.conn.execute(sql, params)]

    def influencer_ids(self) -> set[str]:
        return {r["channel_id"] for r in self.conn.execute("SELECT channel_id FROM influencers")}

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for r in self.conn.execute("SELECT stage, filter_status, COUNT(*) c FROM influencers GROUP BY 1, 2"):
            out[f"{r['stage']}/{r['filter_status']}"] = r["c"]
        return out

    # ------------------------------------------------------------------ messages
    def save_message(self, channel_id: str, **fields_: Any) -> None:
        existing = self.get_message(channel_id)
        data = {
            "email_subject": None,
            "email_body": None,
            "dm_text": None,
            "angle": None,
            "model": None,
            "prompt_version": None,
            "attempts": 0,
            "validation_status": None,
            "validation_notes": None,
            "review_status": "pending",
            "generated_at": now_iso(),
            "reviewed_at": None,
        }
        data.update(fields_)
        if existing is None:
            cols = ["channel_id", *data.keys()]
            self.conn.execute(
                f"INSERT INTO messages ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                [channel_id, *data.values()],
            )
        else:
            sets = ",".join(f"{k}=?" for k in data)
            self.conn.execute(f"UPDATE messages SET {sets} WHERE channel_id=?", [*data.values(), channel_id])
        self.conn.commit()

    def get_message(self, channel_id: str) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM messages WHERE channel_id=?", (channel_id,)).fetchone()
        return dict(row) if row else None

    def list_messages(self, review_status: str | None = None) -> list[dict[str, Any]]:
        if review_status:
            rows = self.conn.execute("SELECT * FROM messages WHERE review_status=? ORDER BY rowid", (review_status,))
        else:
            rows = self.conn.execute("SELECT * FROM messages ORDER BY rowid")
        return [dict(r) for r in rows]

    def set_review_status(self, channel_ids: Iterable[str], status: str) -> int:
        n = 0
        for cid in channel_ids:
            cur = self.conn.execute(
                "UPDATE messages SET review_status=?, reviewed_at=? WHERE channel_id=?", (status, now_iso(), cid)
            )
            n += cur.rowcount
        self.conn.commit()
        return n

    # ------------------------------------------------------------------ outreach log
    def log_outreach(
        self,
        channel_id: str,
        influencer_name: str,
        channel: str,
        status: str,
        recipient: str | None = None,
        delivered_to: str | None = None,
        mode: str | None = None,
        message_ref: str | None = None,
        error: str | None = None,
    ) -> None:
        self.conn.execute(
            """INSERT INTO outreach_log
               (channel_id, influencer_name, recipient, delivered_to, channel, status, mode, message_ref, error, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (
                channel_id,
                influencer_name,
                (recipient or "").lower() or None,
                delivered_to,
                channel,
                status,
                mode,
                message_ref,
                error,
                now_iso(),
            ),
        )
        self.conn.commit()

    def already_contacted(
        self, channel: str, statuses: Iterable[str], channel_id: str, recipient: str | None = None
    ) -> dict[str, Any] | None:
        """Return the earlier log row if this influencer (or this email address) was already
        contacted on this channel with one of the given statuses."""
        statuses = list(statuses)
        ph = ",".join("?" * len(statuses))
        sql = f"SELECT * FROM outreach_log WHERE channel=? AND status IN ({ph}) AND (channel_id=?"
        params: list[Any] = [channel, *statuses, channel_id]
        if recipient:
            sql += " OR recipient=?"
            params.append(recipient.lower())
        sql += ") ORDER BY id LIMIT 1"
        row = self.conn.execute(sql, params).fetchone()
        return dict(row) if row else None

    def outreach_log(self, channel: str | None = None) -> list[dict[str, Any]]:
        if channel:
            rows = self.conn.execute("SELECT * FROM outreach_log WHERE channel=? ORDER BY id", (channel,))
        else:
            rows = self.conn.execute("SELECT * FROM outreach_log ORDER BY id")
        return [dict(r) for r in rows]

    def latest_outreach(self, channel_id: str, channel: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM outreach_log WHERE channel_id=? AND channel=? ORDER BY id DESC LIMIT 1",
            (channel_id, channel),
        ).fetchone()
        return dict(row) if row else None

    def sends_today(self, statuses: Iterable[str]) -> int:
        statuses = list(statuses)
        today = utc_now().date().isoformat()
        row = self.conn.execute(
            f"SELECT COUNT(*) c FROM outreach_log WHERE channel='email' AND status IN ({','.join('?' * len(statuses))})"
            " AND substr(created_at,1,10)=?",
            [*statuses, today],
        ).fetchone()
        return row["c"]

    # ------------------------------------------------------------------ API cache
    def cache_get(self, key: str, max_age_hours: float) -> Any | None:
        row = self.conn.execute("SELECT response, fetched_at FROM api_cache WHERE cache_key=?", (key,)).fetchone()
        if not row:
            return None
        fetched = parse_iso(row["fetched_at"])
        if fetched is None or utc_now() - fetched > timedelta(hours=max_age_hours):
            return None
        return json.loads(row["response"])

    def cache_set(self, key: str, response: Any) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO api_cache (cache_key, response, fetched_at) VALUES (?,?,?)",
            (key, json.dumps(response), now_iso()),
        )
        self.conn.commit()
