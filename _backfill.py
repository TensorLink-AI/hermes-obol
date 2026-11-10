"""Learn this profile's past usage from Hermes's own session log, once, on first use.

Reads the active profile's `state.db` read-only, and only usage columns: each session's time span, model,
billing provider, token counts and cost, plus the timestamps of its assistant replies (never message text).
Each session's cost is spread across the hours its replies landed in, so the forecast sees when this person
actually works. Only time before Obol's own first metered request is imported, so nothing is counted twice.
"""
from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

HOUR = 3600
MAX_DAYS = 90


def state_db(data_dir: Path) -> Path | None:
    """The profile's state.db: the plugin data directory is <profile home>/plugin-data/<id>."""
    home = Path(data_dir).resolve().parent.parent
    p = home / "state.db"
    return p if p.exists() else None


def import_history(store, data_dir: Path, price, now: float) -> dict:
    """Returns {"sessions", "requests", "since", "usd"}; {} when there is nothing to read."""
    db = state_db(data_dir)
    if db is None:
        return {}
    cutoff = float(store.meta("first_call_at") or now)          # Obol's own data starts here
    floor = max(0.0, cutoff - MAX_DAYS * 86400)
    uri = f"file:{db}?mode=ro"
    with closing(sqlite3.connect(uri, uri=True, timeout=5)) as c:
        cols = {r[1] for r in c.execute("PRAGMA table_info(sessions)")}
        need = {"id", "started_at", "model", "input_tokens", "output_tokens"}
        if not need <= cols:
            return {}
        opt = lambda name: name if name in cols else "NULL"  # noqa: E731
        sessions = c.execute(
            f"SELECT id, started_at, {opt('ended_at')}, model, {opt('billing_provider')}, input_tokens, "
            f"output_tokens, {opt('cache_read_tokens')}, {opt('cache_write_tokens')}, "
            f"{opt('actual_cost_usd')}, {opt('estimated_cost_usd')} "
            "FROM sessions WHERE started_at >= ? AND started_at < ?", (floor, cutoff)).fetchall()
        replies = {}
        for sid, ts in c.execute("SELECT session_id, timestamp FROM messages WHERE role='assistant' "
                                 "AND timestamp >= ? AND timestamp < ?", (floor, cutoff)):
            replies.setdefault(sid, []).append(float(ts))
    rows, n_sessions, total = [], 0, 0.0
    for sid, start, end, model, provider, i, o, cr, cw, actual, est in sessions:
        usage = {"input_tokens": i or 0, "output_tokens": o or 0, "cache_read_tokens": cr or 0,
                 "cache_write_tokens": cw or 0}
        if not any(usage.values()) and not (actual or est):
            continue                                            # a session that never reached a model
        actual, est = (None if v is None or v < 0 else v for v in (actual, est))   # -1 is Hermes's "unknown"
        usd = actual if actual is not None else est
        status = "hermes-reported" if actual is not None else "hermes-estimated"
        if usd is None:
            usd, status = price(model, usage)
        times = sorted(replies.get(sid) or [float(start)])
        share = 1.0 / len(times)
        for t in times:
            rows.append(dict(ts=t, session_id=sid, platform="", provider=provider or "", model=model or "",
                             aux_task="history", input_tokens=int(usage["input_tokens"] * share),
                             output_tokens=int(usage["output_tokens"] * share),
                             cache_read_tokens=int(usage["cache_read_tokens"] * share),
                             cache_write_tokens=int(usage["cache_write_tokens"] * share),
                             cost_usd=None if usd is None else float(usd) * share, cost_status=status))
        n_sessions += 1
        total += float(usd or 0)
    if not rows:
        return {}
    store.add_calls(rows)
    since = min(r["ts"] for r in rows)
    store.set_meta("first_call_at", min(since, cutoff))
    return {"sessions": n_sessions, "requests": len(rows), "since": since, "usd": round(total, 4)}
