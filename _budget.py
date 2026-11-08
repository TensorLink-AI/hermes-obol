"""Budgets, periods and what the forecast says about them.

Projection rule (kept honest on purpose): "on track for" adds each remaining hour's expected spend (the mean
of its forecast distribution, estimated as the average of the quantile levels) to what is already spent. Means
add up, so this is the expected period total. Per-hour quantiles are never added up: the forecast draws hours
independently, so a sum of 90% values is not a 90% bound on the total (or on anything).
"tight" means the expected total reaches TIGHT of the budget.
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass
from datetime import datetime, timedelta

HOUR = 3600
TIGHT = 0.85
MODES = ("alert", "save", "stop")
LEGACY = {"watch": "alert", "steer": "save", "guard": "stop"}
MODE_HELP = {"alert": "tells you and the agent when the budget won't last",
             "save": "also switches to a cheaper model when the budget won't last",
             "stop": "also stops new turns once the budget is used up"}
PERIODS = ("day", "week", "month")
ADJ = {"day": "daily", "week": "weekly", "month": "monthly"}


def number(value, default: float = 0.0) -> float:
    """A setting as a usable non-negative amount. Hand-edited config never raises in a hook."""
    try:
        x = float(str(value).strip().lstrip("$+")) if value is not None else default
    except ValueError:
        return default
    return x if math.isfinite(x) and x >= 0 else default


@dataclass
class Budget:
    usd: float = 0.0
    period: str = "week"
    provider: str = ""          # "" = all providers
    mode: str = "alert"         # alert: tell you; save: also switch to cheap_model; stop: also stop at the limit
    cheap_model: str = ""

    @classmethod
    def from_config(cls, get) -> "Budget":
        b = cls(usd=number(get("budget_usd", 0)), period=str(get("period", "week") or "week"),
                provider=str(get("provider", "") or ""), mode=str(get("mode", "alert") or "alert"),
                cheap_model=str(get("cheap_model", "") or ""))
        b.period = b.period if b.period in PERIODS else "week"
        b.mode = LEGACY.get(b.mode, b.mode)
        b.mode = b.mode if b.mode in MODES else "alert"
        return b

    @property
    def active(self) -> bool:
        return self.usd > 0


def period_bounds(period: str, now: float | None = None) -> tuple[float, float]:
    """Local-time calendar periods: today, this week from Monday, this month."""
    now = time.time() if now is None else now
    d = datetime.fromtimestamp(now).replace(hour=0, minute=0, second=0, microsecond=0)
    if period == "day":
        start, end = d, d + timedelta(days=1)
    elif period == "month":
        start = d.replace(day=1)
        end = (start + timedelta(days=32)).replace(day=1)
    else:
        start = d - timedelta(days=d.weekday())
        end = start + timedelta(days=7)
    return start.timestamp(), end.timestamp()


@dataclass
class Outlook:
    spent: float
    usd: float
    start: float
    end: float
    end_median: float | None = None
    out_median: float | None = None     # epoch when expected spend crosses the budget
    tier: str = ""
    model: str = ""

    @property
    def state(self) -> str:
        """out | over (expected to run out) | tight (expected to use TIGHT of it or more) | ok | unknown"""
        if self.spent >= self.usd:
            return "out"
        if self.end_median is None:
            return "unknown"
        if self.end_median >= self.usd:
            return "over"
        if self.end_median >= TIGHT * self.usd:
            return "tight"
        return "ok"


def outlook(budget: Budget, spent: float, spent_this_hour: float, forecast: dict | None,
            now: float | None = None) -> Outlook:
    now = time.time() if now is None else now
    start, end = period_bounds(budget.period, now)
    o = Outlook(spent=spent, usd=budget.usd, start=start, end=end)
    if not forecast:
        return o
    o.tier, o.model = forecast.get("tier", ""), forecast.get("model", "")
    current = int(now // HOUR) * HOUR
    path = forecast.get("mean") or forecast["quantiles"].get("0.5") or []
    total, crossed = spent, None
    for i, v in enumerate(path):
        h = forecast["start"] + i * HOUR
        if v is None or h + HOUR <= now:
            continue
        if h >= end:
            break
        if h == current:
            v = max(0.0, v - spent_this_hour)          # this hour's spend so far is already counted
        before = total
        total += v
        if crossed is None and total >= budget.usd > before and v > 0:
            crossed = max(now, h) + HOUR * (budget.usd - before) / v
    o.end_median, o.out_median = total, crossed
    return o


def _when(ts: float, now: float) -> str:
    dt = datetime.fromtimestamp(ts)
    if ts - now < 20 * HOUR:
        return dt.strftime("%H:%M")
    return dt.strftime("%a %H:%M")


def _money(x: float) -> str:
    if x >= 100:
        return f"${x:,.0f}"
    if x >= 0.01 or x == 0:
        return f"${x:,.2f}"
    return f"${x:.2g}"


def _by_end(o: Outlook) -> str:
    """The period in words: a period ending "Mon 00:00" reads like next week's problem."""
    days = (o.end - o.start) / 86400
    return "today" if days < 2 else "this week" if days < 8 else "this month"


def line(o: Outlook, now: float | None = None) -> str:
    """One plain-language status line, e.g. `$3.10 of $10 · on track to run out ~Thu 15:00`."""
    now = time.time() if now is None else now
    head = f"{_money(o.spent)} of {_money(o.usd)}"
    st = o.state
    if st == "out":
        return f"{head} · budget used up"
    if st == "unknown":
        return f"{head} · too early to forecast (under a day of use)"
    if st == "over":
        tail = f"on track to run out ~{_when(o.out_median, now)}" if o.out_median else "on track to run out"
        return f"{head} · ⚠ {tail}"
    if st == "tight":
        return f"{head} · close: on track for {_money(o.end_median)} {_by_end(o)}"
    return f"{head} · on track for {_money(o.end_median)} {_by_end(o)}"
