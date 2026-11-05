"""Forecasters for hourly spend, best first, chosen by what this machine has.

  ephemeris  Ephemeris API (only when the user has saved a key: that is their opt-in to a network call)
  cascade    Cascade 4M, a 4M-parameter time-series model bundled with the plugin, run in numpy on the CPU
  seasonal   pure Python: each future hour's range from the same hour of day over the last 7 days

Every forecaster returns {"tier", "model", "made_at", "start", "quantiles": {"0.1": [...], "0.5": [...],
"0.9": [...]}} for `horizon` hours starting at the hour after the last completed one. The ranges are
marginal per-hour quantiles: nominal, not a measured coverage.
"""
from __future__ import annotations

import json
import math
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

HOUR = 3600
LEVELS = ("0.1", "0.5", "0.9")
NINE = [0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9]   # the run-out projection averages these
MIN_HOURS_MODEL = 24   # measured on real spend: below a day the all-hours range edges Cascade (12 h: 3.91 vs 4.11
                       # pinball); from 24 h Cascade wins clearly (3.66 vs 6.26). History import usually clears it.


def _quantile(xs, q):
    xs = sorted(xs)
    if not xs:
        return 0.0
    pos = (len(xs) - 1) * q
    lo, hi = math.floor(pos), math.ceil(pos)
    return xs[lo] + (xs[hi] - xs[lo]) * (pos - lo)


# ── seasonal (always available) ──────────────────────────────────────────────

def seasonal(hours, horizon):
    """hours: [(hour_start, usd)] oldest first. Hour-of-day empirical quantiles over the last 7 days.

    With under a day of history there is no daily pattern to read, so every future hour gets the range
    of all hours seen so far.
    """
    values = [v for _, v in hours]
    last = hours[-1][0] if hours else int(time.time() // HOUR) * HOUR - HOUR
    out = {q: [] for q in LEVELS}
    mean = []
    for h in range(1, horizon + 1):
        target = last + h * HOUR
        if len(values) >= 24:
            same = [v for t, v in hours[-24 * 7:] if (target - t) % (24 * HOUR) == 0]
        else:
            same = values
        same = same or [0.0]
        for q in LEVELS:
            out[q].append(max(0.0, _quantile(same, float(q))))
        mean.append(max(0.0, sum(same) / len(same)))
    return {"tier": "seasonal", "model": "hour-of-day quantiles, last 7 days", "made_at": time.time(),
            "start": last + HOUR, "quantiles": out, "mean": mean}


# ── cascade (local, numpy) ───────────────────────────────────────────────────

def cascade_status(data_dir: Path = None) -> tuple[bool, str]:
    """(usable, why not). Cascade 4M ships with the plugin; it needs only numpy."""
    try:
        import numpy  # noqa: F401
    except Exception:
        return False, "numpy isn't installed for Hermes"
    from . import _cascade
    if not _cascade.WEIGHTS.exists():
        return False, "Cascade 4M weights are missing from the plugin"
    return True, ""


def cascade(hours, horizon, data_dir: Path = None):
    from . import _cascade
    q = _cascade.model().quantiles([v for _, v in hours], horizon)
    q = q.clip(min=0.0)
    lv = _cascade.model().levels
    pick = {f"{lv[i]:g}": i for i in range(len(lv))}
    return {"tier": "cascade", "model": "Cascade 4M (local, CPU)", "made_at": time.time(),
            "start": hours[-1][0] + HOUR, "quantiles": {k: q[:, pick[k]].tolist() for k in LEVELS},
            "mean": q.mean(axis=1).tolist()}


# ── ephemeris (opt-in network) ───────────────────────────────────────────────

EPHEMERIS_API = "https://ephemeris.cascade.industries/api/v1"


def ephemeris(hours, horizon, key: str, model: str = "toto2-313m", timeout: float = 60):
    """One paid call. Sends only the hourly dollar totals: no prompts, models, sessions or names.
    No automatic retries; the Idempotency-Key is chosen before submission."""
    body = {"series": [{"values": [round(v, 6) for _, v in hours][-2048:], "freq": "H"}],
            "horizon": horizon, "quantiles": NINE}
    body.update({"mode": "route"} if model in ("", "route") else {"mode": "explicit", "model": model})
    req = urllib.request.Request(f"{EPHEMERIS_API}/forecast", data=json.dumps(body).encode(), method="POST",
                                 headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json",
                                          "Idempotency-Key": f"obol-{uuid.uuid4()}",
                                          "User-Agent": "hermes-obol"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        got = json.load(r)
    qs = got["forecasts"][0]["quantiles"]
    def path(lv):
        return [max(0.0, float(x)) if x is not None else None for x in (qs.get(lv) or [])][:horizon]
    out = {lv: path(lv) for lv in LEVELS}
    nine = [path(f"{q:g}") for q in NINE]
    mean = [None if any(p[i] is None for p in nine if len(p) > i) or not all(len(p) > i for p in nine)
            else sum(p[i] for p in nine) / len(nine) for i in range(horizon)]
    billing = (got.get("meta") or {}).get("billing") or {}
    return {"tier": "ephemeris", "model": f"Ephemeris {model or 'route'}", "made_at": time.time(),
            "start": hours[-1][0] + HOUR, "quantiles": out, "mean": mean,
            "settled_mc": billing.get("settled_mc"), "balance_mc": billing.get("balance_mc")}


# ── choosing ─────────────────────────────────────────────────────────────────

def choose(setting: str, have_key: bool, data_dir: Path, n_hours: int) -> tuple[str, str]:
    """(tier, note). setting: auto | ephemeris | cascade | seasonal."""
    if n_hours < MIN_HOURS_MODEL and setting in ("auto", "ephemeris", "cascade"):
        return "seasonal", f"{n_hours}h of history so far; models start at {MIN_HOURS_MODEL}h"
    if setting in ("auto", "ephemeris") and have_key:
        return "ephemeris", ""
    ok, why = cascade_status(data_dir)
    if setting in ("auto", "cascade", "ephemeris") and ok:
        return "cascade", "" if setting != "ephemeris" else "no Ephemeris key saved"
    if setting == "seasonal":
        return "seasonal", ""
    return "seasonal", why if setting in ("auto", "cascade") else "no Ephemeris key saved"
