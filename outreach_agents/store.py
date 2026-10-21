"""Storage. Everything the agents keep goes through this small interface; SQLite is the default.

Swap in your own (Postgres, Supabase...) by implementing the same methods and passing it around.
"""
from __future__ import annotations

import datetime as dt
import json
import secrets
import sqlite3
import threading
import uuid
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS prospects (
  id TEXT PRIMARY KEY, name TEXT, website TEXT UNIQUE, osm_id TEXT, postcode TEXT, city TEXT, sector TEXT,
  email TEXT, company_number TEXT, legal_name TEXT, status TEXT, reject_reason TEXT, step INTEGER DEFAULT 0,
  optout_token TEXT, demo_slug TEXT, next_send_at TEXT, last_sent_at TEXT, replied_at TEXT, outcome TEXT,
  notes TEXT, created_at TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT, prospect_id TEXT, direction TEXT, kind TEXT, step INTEGER,
  subject TEXT, body TEXT, message_id TEXT, status TEXT, error TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS suppression (value TEXT PRIMARY KEY, reason TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS demos (
  slug TEXT PRIMARY KEY, prospect_id TEXT, business_name TEXT, site_url TEXT, welcome TEXT, suggestions TEXT,
  chunks TEXT, status TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT);
"""


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


class Store:
    def __init__(self, path: str | Path = ":memory:"):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.lock = threading.Lock()
        with self.lock:
            self.db.executescript(SCHEMA)

    def _q(self, sql: str, args=()) -> list[dict]:
        with self.lock:
            cur = self.db.execute(sql, args)
            self.db.commit()
            return [dict(r) for r in cur.fetchall()]

    # --- prospects ---------------------------------------------------------------------------------
    def add_prospect(self, **row) -> str | None:
        """Insert a prospect; None if that website is already known."""
        row = {"id": uuid.uuid4().hex, "optout_token": secrets.token_urlsafe(18), "created_at": now(),
               "updated_at": now(), **row}
        cols = ",".join(row)
        try:
            self._q(f"INSERT INTO prospects ({cols}) VALUES ({','.join('?' * len(row))})", tuple(row.values()))
        except sqlite3.IntegrityError:
            return None
        return row["id"]

    def update(self, pid: str, **changes) -> None:
        changes["updated_at"] = now()
        sets = ",".join(f"{k}=?" for k in changes)
        self._q(f"UPDATE prospects SET {sets} WHERE id=?", (*changes.values(), pid))

    def prospect(self, pid: str) -> dict | None:
        rows = self._q("SELECT * FROM prospects WHERE id=?", (pid,))
        return rows[0] if rows else None

    def by_email(self, email: str) -> dict | None:
        rows = self._q("SELECT * FROM prospects WHERE lower(email)=lower(?) LIMIT 1", (email,))
        return rows[0] if rows else None

    def by_token(self, token: str) -> dict | None:
        rows = self._q("SELECT * FROM prospects WHERE optout_token=? LIMIT 1", (token,))
        return rows[0] if rows else None

    def by_status(self, status: str, limit: int = 100, order: str = "created_at") -> list[dict]:
        return self._q(f"SELECT * FROM prospects WHERE status=? ORDER BY {order} LIMIT ?", (status, limit))

    def count(self, status: str) -> int:
        return self._q("SELECT count(*) n FROM prospects WHERE status=?", (status,))[0]["n"]

    def known(self) -> tuple[set[str], set[str]]:
        rows = self._q("SELECT osm_id, website FROM prospects")
        return {r["osm_id"] for r in rows if r["osm_id"]}, {r["website"] for r in rows if r["website"]}

    def due(self, limit: int) -> list[dict]:
        """Follow-ups that are due first, then approved drafts, then fresh prospects."""
        rows = self._q("SELECT * FROM prospects WHERE status='contacted' AND next_send_at<=? "
                       "ORDER BY next_send_at LIMIT ?", (now(), limit))
        for status in ("approved", "ready"):
            if len(rows) < limit:
                rows += self.by_status(status, limit - len(rows))
        return rows

    # --- messages ----------------------------------------------------------------------------------
    def log_message(self, prospect_id: str | None, direction: str, kind: str, **f) -> None:
        row = {"prospect_id": prospect_id, "direction": direction, "kind": kind, "created_at": now(), **f}
        self._q(f"INSERT INTO messages ({','.join(row)}) VALUES ({','.join('?' * len(row))})", tuple(row.values()))

    def first_sent(self, pid: str) -> dict | None:
        rows = self._q("SELECT subject, message_id FROM messages WHERE prospect_id=? AND direction='out' "
                       "AND status='sent' ORDER BY id LIMIT 1", (pid,))
        return rows[0] if rows else None

    def prospect_for_message(self, message_id: str) -> str | None:
        rows = self._q("SELECT prospect_id FROM messages WHERE message_id=? LIMIT 1", (message_id,))
        return rows[0]["prospect_id"] if rows else None

    def recent_sent(self, n: int) -> list[str]:
        return [r["prospect_id"] for r in self._q(
            "SELECT prospect_id FROM messages WHERE direction='out' AND status='sent' ORDER BY id DESC LIMIT ?", (n,))]

    # --- suppression: checked before every send -------------------------------------------------------
    def suppressed(self, email: str) -> bool:
        e = (email or "").lower().strip()
        dom = "@" + e.rpartition("@")[2]
        return bool(self._q("SELECT 1 FROM suppression WHERE value IN (?, ?)", (e, dom)))

    def suppress(self, email: str, reason: str) -> None:
        if email:
            self._q("INSERT OR REPLACE INTO suppression VALUES (?, ?, ?)", (email.lower().strip(), reason[:200], now()))

    # --- demos -------------------------------------------------------------------------------------
    def save_demo(self, slug: str, **d) -> None:
        d = {"slug": slug, "created_at": now(), **d}
        for k in ("suggestions", "chunks"):
            if k in d and not isinstance(d[k], str):
                d[k] = json.dumps(d[k])
        self._q(f"INSERT OR REPLACE INTO demos ({','.join(d)}) VALUES ({','.join('?' * len(d))})", tuple(d.values()))

    def demo(self, slug: str) -> dict | None:
        rows = self._q("SELECT * FROM demos WHERE slug=?", (slug,))
        if not rows:
            return None
        d = rows[0]
        for k in ("suggestions", "chunks"):
            d[k] = json.loads(d[k] or "[]")
        return d

    # --- small state (sending counters, inbox position) --------------------------------------------------
    def get_state(self, key: str, default=None):
        rows = self._q("SELECT value FROM kv WHERE key=?", (key,))
        return json.loads(rows[0]["value"]) if rows else default

    def set_state(self, key: str, value) -> None:
        self._q("INSERT OR REPLACE INTO kv VALUES (?, ?)", (key, json.dumps(value)))
