"""Budgets, periods and what the forecast says about them.

Projection rules (kept honest on purpose):
  * "at forecast pace" adds each hour's expected spend (the mean of the forecast distribution, estimated
    as the average of its quantile levels) to what is already spent. Means add up, so this is the
    expected period total; a sum of medians would not be, and on real bursty spend it ran 25-60% low.
  * "at the high pace" adds the per-hour 0.9 quantiles. That is a deliberately pessimistic path
    (every hour at its high end), not a 90% bound on the total.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timedelta

HOUR = 3600
MODES = ("watch", "steer", "guard")
PERIODS = ("day", "week", "month")
ADJ = {"day": "daily", "week": "weekly", "month": "monthly"}


@dataclass
class Budget:
    usd: float = 0.0
    period: str = "week"
    provider: str = ""          # "" = all providers
    mode: str = "watch"         # watch: tell you; steer: also switch to cheap_model; guard: also stop at the limit
    cheap_model: str = ""

    @classmethod
    def from_config(cls, get) -> "Budget":
        b = cls(usd=float(get("budget_usd", 0) or 0), period=str(get("period", "week") or "week"),
                provider=str(get("provider", "") or ""), mode=str(get("mode", "watch") or "watch"),
                cheap_model=str(get("cheap_model", "") or ""))
        b.period = b.period if b.period in PERIODS else "week"
        b.mode = b.mode if b.mode in MODES else "watch"
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
    end_high: float | None = None
    out_median: float | None = None     # epoch when spend crosses the budget at forecast pace
    out_high: float | None = None       # ... at the high pace
    tier: str = ""
    model: str = ""

    @property
    def state(self) -> str:
        """out | over (on pace to run out) | tight (only the high pace runs out) | ok | unknown"""
        if self.spent >= self.usd:
            return "out"
        if self.end_median is None:
            return "unknown"
        if self.end_median >= self.usd:
            return "over"
        if self.end_high is not None and self.end_high >= self.usd:
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
    expected = forecast.get("mean") or forecast["quantiles"].get("0.5") or []
    for path, attr_end, attr_out in ((expected, "end_median", "out_median"),
                                     (forecast["quantiles"].get("0.9") or [], "end_high", "out_high")):
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
        setattr(o, attr_end, total)
        setattr(o, attr_out, crossed)
    return o


def _when(ts: float, now: float) -> str:
    dt = datetime.fromtimestamp(ts)
    if ts - now < 20 * HOUR:
        return dt.strftime("%H:%M")
    return dt.strftime("%a %H:%M")


def _money(x: float) -> str:
    if x >= 100:
        return f"${x:,.0f}"
    if x >= 1 or x == 0:
        return f"${x:,.2f}"
    return f"${x:.2g}" if x < 0.01 else f"${x:.3f}".rstrip("0")


def line(o: Outlook, now: float | None = None) -> str:
    """One status line, e.g. `$3.10 of $10 this week · out ~Thu 15:00 (high pace Wed 09:00)`."""
    now = time.time() if now is None else now
    head = f"{_money(o.spent)} of {_money(o.usd)}"
    st = o.state
    if st == "out":
        return f"{head} · budget used up"
    if st == "unknown":
        return f"{head} · first forecast after a full hour of use"
    if st == "over":
        tail = f"out ~{_when(o.out_median, now)}" if o.out_median else "on pace to run out"
        if o.out_high and o.out_high < (o.out_median or o.end):
            tail += f" (high pace {_when(o.out_high, now)})"
        return f"{head} · ⚠ {tail}"
    if st == "tight":
        return (f"{head} · on pace for {_money(o.end_median)}; at the high pace out ~{_when(o.out_high, now)}"
                if o.out_high else f"{head} · tight")
    return f"{head} · on pace for {_money(o.end_median)} by {_when(o.end, now)}"
