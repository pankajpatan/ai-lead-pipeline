"""SQLite storage: leads with a state machine, and an event log for every step."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS leads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    idem_key TEXT NOT NULL UNIQUE,
    source TEXT NOT NULL,
    name TEXT NOT NULL,
    email TEXT NOT NULL,
    company TEXT,
    message TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'received',
    tier TEXT,
    score INTEGER,
    qualification TEXT,
    qualifier TEXT,
    error TEXT,
    resume_state TEXT,
    crm TEXT,
    draft TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    lead_id INTEGER NOT NULL REFERENCES leads(id),
    kind TEXT NOT NULL,
    detail TEXT,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_lead ON events(lead_id, id);
CREATE INDEX IF NOT EXISTS idx_leads_state ON leads(state);
"""

# Allowed moves. Later steps fill in CRM sync, drafting, approval and sending.
TRANSITIONS: dict[str, set[str]] = {
    "received": {"qualified", "needs_attention", "rejected"},
    "qualified": {"crm_synced", "needs_attention", "rejected"},
    "crm_synced": {"draft_ready", "needs_attention"},
    "draft_ready": {"approved", "rejected", "needs_attention"},
    "approved": {"sent", "needs_attention"},
    "needs_attention": {"received", "qualified", "crm_synced", "approved", "rejected"},
    "sent": set(),
    "rejected": set(),
}
STATES = set(TRANSITIONS)


JSON_FIELDS = ("qualification", "crm", "draft")
FIELDS = {"tier", "score", "qualification", "qualifier", "error", "attempts", "resume_state", "crm", "draft"}


class InvalidTransition(ValueError):
    pass


class Store:
    def __init__(self, path: str):
        self._lock = threading.RLock()
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA foreign_keys = ON")
        self._db.executescript(SCHEMA)
        # Databases created by step 1 lack the newer columns.
        have = {r["name"] for r in self._db.execute("PRAGMA table_info(leads)")}
        for column in ("resume_state", "crm", "draft"):
            if column not in have:
                self._db.execute(f"ALTER TABLE leads ADD COLUMN {column} TEXT")
        self._db.commit()

    def add_lead(self, idem_key: str, source: str, name: str, email: str, company: str | None,
                 message: str) -> tuple[int, bool]:
        """Insert a lead. Returns (lead_id, created). A repeated idem_key returns the first lead."""
        now = time.time()
        with self._lock:
            try:
                cur = self._db.execute(
                    "INSERT INTO leads (idem_key, source, name, email, company, message, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (idem_key, source, name, email, company, message, now, now),
                )
            except sqlite3.IntegrityError:
                row = self._db.execute("SELECT id FROM leads WHERE idem_key = ?", (idem_key,)).fetchone()
                self._log(row["id"], "duplicate_delivery", None, now)
                self._db.commit()
                return row["id"], False
            lead_id = cur.lastrowid
            self._log(lead_id, "received", source, now)
            self._db.commit()
            return lead_id, True

    def update(self, lead_id: int, **fields: Any) -> None:
        """Change fields without changing state (for progress saved inside a step)."""
        fields = _encode(fields)
        sets = ", ".join(f"{k} = ?" for k in fields)
        with self._lock:
            self._db.execute(f"UPDATE leads SET {sets}, updated_at = ? WHERE id = ?",
                             (*fields.values(), time.time(), lead_id))
            self._db.commit()

    def transition(self, lead_id: int, new_state: str, detail: Any = None, **fields: Any) -> None:
        fields = _encode(fields)
        now = time.time()
        with self._lock:
            row = self._db.execute("SELECT state FROM leads WHERE id = ?", (lead_id,)).fetchone()
            if row is None:
                raise KeyError(lead_id)
            if new_state not in TRANSITIONS[row["state"]]:
                raise InvalidTransition(f"{row['state']} -> {new_state}")
            sets = ", ".join(f"{k} = ?" for k in fields)
            self._db.execute(
                f"UPDATE leads SET state = ?, {sets + ', ' if sets else ''}updated_at = ? WHERE id = ?",
                (new_state, *fields.values(), now, lead_id),
            )
            self._log(lead_id, new_state, detail, now)
            self._db.commit()

    def bump_attempts(self, lead_id: int) -> int:
        with self._lock:
            self._db.execute("UPDATE leads SET attempts = attempts + 1 WHERE id = ?", (lead_id,))
            self._db.commit()
            return self._db.execute("SELECT attempts FROM leads WHERE id = ?", (lead_id,)).fetchone()[0]

    def log(self, lead_id: int, kind: str, detail: Any = None) -> None:
        with self._lock:
            self._log(lead_id, kind, detail, time.time())
            self._db.commit()

    def _log(self, lead_id: int, kind: str, detail: Any, now: float) -> None:
        if detail is not None and not isinstance(detail, str):
            detail = json.dumps(detail)
        self._db.execute(
            "INSERT INTO events (lead_id, kind, detail, created_at) VALUES (?, ?, ?, ?)",
            (lead_id, kind, detail, now),
        )

    def get(self, lead_id: int) -> dict[str, Any] | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM leads WHERE id = ?", (lead_id,)).fetchone()
        return _decode(row) if row else None

    def leads(self, state: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
        query, args = "SELECT * FROM leads", []
        if state:
            query, args = query + " WHERE state = ?", [state]
        with self._lock:
            rows = self._db.execute(query + " ORDER BY id DESC LIMIT ?", (*args, limit)).fetchall()
        return [_decode(r) for r in rows]

    def events(self, lead_id: int) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._db.execute(
                "SELECT kind, detail, created_at FROM events WHERE lead_id = ? ORDER BY id", (lead_id,)
            ).fetchall()
        return [dict(r) for r in rows]

    def reset(self) -> None:
        with self._lock:
            self._db.executescript("DELETE FROM events; DELETE FROM leads;")
            self._db.commit()


def _encode(fields: dict[str, Any]) -> dict[str, Any]:
    unknown = set(fields) - FIELDS
    if unknown:
        raise ValueError(f"Unknown lead fields: {sorted(unknown)}")
    return {k: json.dumps(v) if k in JSON_FIELDS and v is not None and not isinstance(v, str) else v
            for k, v in fields.items()}


def _decode(row: sqlite3.Row) -> dict[str, Any]:
    lead = dict(row)
    for key in JSON_FIELDS:
        if lead.get(key):
            lead[key] = json.loads(lead[key])
    return lead
