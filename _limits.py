"""Claude and Codex plan limits: how full each window is, and whether it fills before it resets.

Hermes reads the window from the vendor with the login it already holds (`agent.account_usage`, the public surface
for this); Obol never sees a credential. The vendor's figure counts every client on the account, not just Hermes.

Projection: the window's percent so far, per token Hermes sent in it, applied to the token forecast for the hours
until it resets (means add up; per-hour quantiles are never added). If Hermes sent next to nothing in the window, the
use came from somewhere else and the token forecast says nothing about it, so fall back to the pace so far.
"""
from __future__ import annotations

from ._rules import SEVERITY

HOUR = 3600
PROVIDERS = ("anthropic", "openai-codex")
NAMES = {"anthropic": "Claude", "openai-codex": "Codex"}
MIN_TOKENS = 10_000             # below this, Hermes's own share of the window is too small to scale from
MIN_ELAPSED = 0.05              # too early in a window to read a pace
TIGHT = 85.0


def read(provider: str) -> dict:
    """{"windows": [...], "error": str|None}. Network, through Hermes. Call off the request path."""
    from agent.account_usage import fetch_account_usage
    try:
        snap = fetch_account_usage(provider, read_only=True)
    except Exception as e:
        return {"windows": [], "error": f"couldn't read it ({type(e).__name__})"}
    if snap is None:
        return {"windows": [], "error": "not signed in to Hermes"}
    if getattr(snap, "unavailable_reason", None):
        return {"windows": [], "error": str(snap.unavailable_reason)[:160]}
    rows = [{"label": w.label, "used": float(w.used_percent), "scope": getattr(w, "scope", "account"),
             "reset_at": w.reset_at.timestamp() if w.reset_at else None}
            for w in snap.windows if w.used_percent is not None]
    return {"windows": rows, "error": None}


def length(label: str) -> float | None:
    """A window's length, from its name: the vendors report when it resets, not how long it is."""
    text = (label or "").lower()
    if "session" in text or "5h" in text or "5-hour" in text or "5 hour" in text:
        return 5 * HOUR
    if "week" in text or "7d" in text:
        return 7 * 86400
    if "month" in text:
        return 30 * 86400
    if "day" in text:
        return 86400
    return None


def project(used: float, now: float, reset_at: float | None, size: float | None, tokens_in: float,
            path: list | None, path_start: float | None, tokens_this_hour: float = 0.0) -> dict:
    """{"expected": percent at reset or None, "out_at": when it reaches 100 or None, "how": "forecast"|"pace"|""}."""
    out = {"expected": None, "out_at": None, "how": ""}
    if used >= 100 or not reset_at or reset_at <= now:
        return out
    if path and path_start is not None and tokens_in >= MIN_TOKENS and used > 0:
        rate, total, current = used / tokens_in, used, int(now // HOUR) * HOUR
        for i, tok in enumerate(path):
            h = path_start + i * HOUR
            if tok is None or h + HOUR <= now:
                continue
            if h >= reset_at:
                break
            tok = max(0.0, tok - tokens_this_hour) if h == current else tok
            if h + HOUR > reset_at:                 # only part of this hour falls inside the window
                tok *= (reset_at - max(h, now)) / (h + HOUR - max(h, now))
            add = tok * rate
            if out["out_at"] is None and total + add >= 100 > total and add > 0:
                out["out_at"] = max(now, h) + (min(h + HOUR, reset_at) - max(now, h)) * (100 - total) / add
            total += add
        out.update(expected=total, how="forecast")
        return out
    if size:
        elapsed = size - (reset_at - now)
        if elapsed >= MIN_ELAPSED * size and used > 0:
            per_second = used / elapsed
            expected = used + per_second * (reset_at - now)
            out.update(expected=expected, how="pace")
            if expected >= 100:
                out["out_at"] = now + (100 - used) / per_second
    return out


def state(used: float, expected: float | None) -> str:
    if used >= 100:
        return "out"
    top = max(used, expected if expected is not None else used)
    return "over" if top >= 100 else "tight" if top >= TIGHT else "ok"


def worst_first(rows: list) -> list:
    return sorted(rows, key=lambda r: (SEVERITY.get(r["state"], 0), -(r["out_at"] or float("inf"))), reverse=True)
