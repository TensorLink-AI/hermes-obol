"""Obol's ledger: one row per provider request (tokens and cost, never prompt or reply text).

Lives in this plugin's own data directory under the active Hermes profile. SQLite in WAL mode,
a short-lived connection per call, so the CLI, gateway and cron processes can share it.
"""
from __future__ import annotations

import json
import sqlite3
import time
from contextlib import closing
from pathlib import Path

HOUR = 3600

_SCHEMA = """
CREATE TABLE IF NOT EXISTS calls (
  ts REAL NOT NULL,
  session_id TEXT, platform TEXT, provider TEXT, model TEXT, aux_task TEXT,
  input_tokens INTEGER, output_tokens INTEGER, cache_read_tokens INTEGER,
  cache_write_tokens INTEGER, reasoning_tokens INTEGER,
  cost_usd REAL, cost_status TEXT
);
CREATE INDEX IF NOT EXISTS calls_ts ON calls(ts);
CREATE INDEX IF NOT EXISTS calls_provider_ts ON calls(provider, ts);
CREATE INDEX IF NOT EXISTS calls_session_ts ON calls(session_id, ts);
CREATE INDEX IF NOT EXISTS calls_model_ts ON calls(model, ts);
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT NOT NULL);
"""


class Store:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._hourly = {}             # completed hours don't change, and the hooks ask on every call
        with closing(self._conn()) as c, c:
            c.execute("PRAGMA journal_mode=WAL")
            c.executescript(_SCHEMA)

    def _conn(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path, timeout=5)

    # ── calls ────────────────────────────────────────────────────────────────
    def add_call(self, *, ts=None, session_id="", platform="", provider="", model="", aux_task="",
                 input_tokens=0, output_tokens=0, cache_read_tokens=0, cache_write_tokens=0,
                 reasoning_tokens=0, cost_usd=None, cost_status="unknown") -> None:
        ts = time.time() if ts is None else ts
        with closing(self._conn()) as c, c:
            c.execute("INSERT INTO calls VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                      (ts, session_id, platform, provider, model, aux_task, int(input_tokens or 0),
                       int(output_tokens or 0), int(cache_read_tokens or 0), int(cache_write_tokens or 0),
                       int(reasoning_tokens or 0), cost_usd, cost_status))
        if any(ts < k[3] for k in list(self._hourly)):      # a row for an hour already cached
            self._hourly.clear()
        first = self.meta("first_call_at")
        if first is None or ts < float(first):
            self.set_meta("first_call_at", ts)

    def reprice(self, price) -> int:
        """Fill in costs for requests that had no price when they were metered. price(model, usage)."""
        with closing(self._conn()) as c, c:
            rows = c.execute("SELECT rowid, model, input_tokens, output_tokens, cache_read_tokens, "
                             "cache_write_tokens FROM calls WHERE cost_usd IS NULL").fetchall()
            n = 0
            for rowid, model, i, o, cr, cw in rows:
                usd, status = price(model, {"input_tokens": i, "output_tokens": o, "cache_read_tokens": cr,
                                            "cache_write_tokens": cw})
                if usd is not None:
                    c.execute("UPDATE calls SET cost_usd=?, cost_status=? WHERE rowid=?", (usd, status, rowid))
                    n += 1
        self._hourly.clear()
        return n

    def add_calls(self, rows) -> None:
        """Bulk insert (history import); leaves first_call_at to the caller."""
        cols = ("ts", "session_id", "platform", "provider", "model", "aux_task", "input_tokens", "output_tokens",
                "cache_read_tokens", "cache_write_tokens", "reasoning_tokens", "cost_usd", "cost_status")
        with closing(self._conn()) as c, c:
            c.executemany("INSERT INTO calls VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                          [tuple(r.get(k, 0 if k.endswith("tokens") else "") for k in cols) for r in rows])
        self._hourly.clear()

    def _scope(self, provider: str):
        return ("AND provider = ?", (provider,)) if provider else ("", ())

    def spent(self, since: float, until: float | None = None, provider: str = "") -> float:
        until = time.time() if until is None else until
        where, args = self._scope(provider)
        with closing(self._conn()) as c:
            row = c.execute(f"SELECT COALESCE(SUM(cost_usd),0) FROM calls WHERE ts>=? AND ts<? {where}",
                            (since, until, *args)).fetchone()
        return float(row[0])

    def unpriced(self, since: float, provider: str = "") -> int:
        """Requests in the window whose cost Hermes couldn't price (counted as $0, so spend is a floor)."""
        where, args = self._scope(provider)
        with closing(self._conn()) as c:
            return int(c.execute(f"SELECT COUNT(*) FROM calls WHERE ts>=? AND cost_usd IS NULL {where}",
                                 (since, *args)).fetchone()[0])

    def hourly_tokens(self, until: float | None = None, provider: str = "", max_hours: int = 4096):
        """Same grid as hourly(), but total tokens (input + cache read + cache write + output) per hour."""
        return self.hourly(until, provider, max_hours, column="input_tokens + cache_read_tokens + "
                                                              "cache_write_tokens + output_tokens")

    def hourly(self, until: float | None = None, provider: str = "", max_hours: int = 2048,
               column: str = "COALESCE(cost_usd,0)"):
        """Completed hours of spend, oldest first: (hour_start, usd).

        Starts at the hour of the first metered call (hours before Obol was watching are unknown and are
        never sent as zeros); after that an hour with no requests really did cost $0.
        """
        until = time.time() if until is None else until
        end = int(until // HOUR) * HOUR                      # the current, incomplete hour is excluded
        first = self.meta("first_call_at")
        if first is None:
            return []
        start = max(int(float(first) // HOUR) * HOUR, end - max_hours * HOUR)
        key = (provider, column, start, end)
        if key in self._hourly:
            return self._hourly[key]
        where, args = self._scope(provider)
        with closing(self._conn()) as c:
            rows = c.execute(f"SELECT CAST(ts/{HOUR} AS INTEGER)*{HOUR} h, SUM({column}) "
                             f"FROM calls WHERE ts>=? AND ts<? {where} GROUP BY h", (start, end, *args)).fetchall()
        got = {int(h): float(v) for h, v in rows}
        out = [(h, got.get(h, 0.0)) for h in range(start, end, HOUR)]
        if len(self._hourly) > 32:
            self._hourly.clear()
        self._hourly[key] = out
        return out

    def last_provider(self, session_id: str) -> str:
        with closing(self._conn()) as c:
            row = c.execute("SELECT provider FROM calls WHERE session_id=? AND aux_task='' ORDER BY ts DESC LIMIT 1",
                            (session_id,)).fetchone()
        return row[0] if row else ""

    def provider_of_model(self, model: str) -> str:
        """The provider this model last went through: a new session's best hint before its first request."""
        if not model:
            return ""
        with closing(self._conn()) as c:
            row = c.execute("SELECT provider FROM calls WHERE model=? AND provider NOT IN ('', 'auto') "
                            "ORDER BY ts DESC LIMIT 1", (model,)).fetchone()
        return row[0] if row else ""

    def latest_provider(self) -> str:
        with closing(self._conn()) as c:
            row = c.execute("SELECT provider FROM calls WHERE aux_task='' AND provider NOT IN ('', 'auto') "
                            "ORDER BY ts DESC LIMIT 1").fetchone()
        return row[0] if row else ""

    def providers(self, since: float) -> list:
        with closing(self._conn()) as c:
            return [r[0] for r in c.execute("SELECT DISTINCT provider FROM calls WHERE ts>=? AND provider NOT IN "
                                            "('', 'auto')", (since,))]

    def last_prompt_tokens(self, session_id: str) -> int:
        """Prompt size (input + cache) of this session's latest main-loop request; 0 for a new session."""
        if not session_id:
            return 0
        with closing(self._conn()) as c:
            row = c.execute("SELECT input_tokens + cache_read_tokens + cache_write_tokens FROM calls "
                            "WHERE session_id=? AND aux_task='' ORDER BY ts DESC LIMIT 1", (session_id,)).fetchone()
        return int(row[0]) if row and row[0] else 0

    def by_model(self, since: float, provider: str = ""):
        where, args = self._scope(provider)
        with closing(self._conn()) as c:
            return c.execute(f"SELECT model, COUNT(*), SUM(COALESCE(cost_usd,0)) FROM calls WHERE ts>=? {where} "
                             "GROUP BY model ORDER BY 3 DESC", (since, *args)).fetchall()

    # ── meta ─────────────────────────────────────────────────────────────────
    def meta(self, key: str, default=None):
        with closing(self._conn()) as c:
            row = c.execute("SELECT v FROM meta WHERE k=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set_meta(self, key: str, value) -> None:
        with closing(self._conn()) as c, c:
            c.execute("INSERT INTO meta VALUES (?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                      (key, json.dumps(value)))
