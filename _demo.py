"""/obol demo: a made-up week, run through the real forecast and rules, in a temporary ledger.

Nothing about your own usage is read or saved. Five ordinary weeks, then a heavy one, with a budget a little above a
usual week. The week plays out twice from the same usage: once as it would with no plugin, and once with Obol deciding
every few working hours, so the difference is what save mode actually did. The moment Obol first sees the week running
out is drawn in the reply, and on the Desktop chip for a few minutes, labelled as made up.
"""
from __future__ import annotations

import random
import tempfile
from datetime import datetime

from . import _budget
from ._store import HOUR

BIG, SMALL = "demo/big-model", "demo/small-model"
PRICES = {BIG: [3.0, 15.0, 0.3, 3.75], SMALL: [0.14, 0.28, 0.014, 0.14]}    # $ per million tokens
WORK_HOURS = range(9, 18)
CHECK_HOURS = (9, 12, 15)
HEAVY = 1.7                     # this week's usage against a usual one
WEEKS = 5                       # ordinary weeks before it: enough for the chart's range of how weeks vary
BAR = 20                        # characters that stand for the whole limit


def _usage(rng, scale=1.0) -> dict:
    prompt = rng.randint(120_000, 260_000) * scale
    return {"input_tokens": int(prompt * 0.3), "cache_read_tokens": int(prompt * 0.7),
            "cache_write_tokens": 0, "output_tokens": int(rng.randint(2_000, 6_000) * scale)}


def _working(start: float, end: float):
    """The working hours from start to end, Monday to Friday."""
    for h in range(int(start // HOUR) * HOUR, int(end), HOUR):
        d = datetime.fromtimestamp(h)
        if d.weekday() < 5 and d.hour in WORK_HOURS:
            yield h


def _bars(chart: dict) -> list:
    """The week as text, for chats that can't draw: solid is spent, light is what the forecast expected next."""
    now, limit, rows, over = chart["actual"][-1][0], chart["limit"], [], False
    for d in range(7):
        end = chart["start"] + (d + 1) * 86400
        spent = [v for t, v in chart["actual"] if t <= end][-1]
        ahead = [v for t, v in chart["expected"] if t <= end]
        value = ahead[-1] if ahead and end > now else spent
        solid, light = round(spent / limit * BAR), round(max(0.0, value - spent) / limit * BAR)
        note = "  ← over the limit" if value > limit and not over else ""
        over = over or value > limit
        day = datetime.fromtimestamp(end - 43200)
        rows.append(f"  {day:%a} {'█' * solid}{'░' * light} {_budget._money(value)}{note}")
    return rows


def _chip(snapshot: dict) -> dict:
    return dict(snapshot, state="demo", demo=True, chip=f"Demo · {snapshot['chip']}", lines=[
        "Made-up usage from /obol demo. Your real numbers come back in 5 minutes.", *snapshot["lines"][:1]],
        rows=[dict(r, now=snapshot["at"]) for r in snapshot.get("rows", [])])   # bars follow the demo's clock, not today's


def run(obol_cls, now: float, show=None) -> str:
    rng = random.Random(7)
    monday, sunday = _budget.period_bounds("week", now)
    settings = {"mode": "save", "cheap_model": SMALL, "prices": PRICES, "unusual_day_x": 0}
    sim = {"now": monday}
    with tempfile.TemporaryDirectory(prefix="obol-demo-") as tmp:
        o = obol_cls(data_dir=tmp, get=lambda k, d=None: settings.get(k, d), set_=settings.__setitem__,
                     secret=lambda n: None, clock=lambda: sim["now"])
        o.prices.stale = lambda now=None: False            # no price fetch
        o.store.set_meta("history_imported", {"sessions": 0})   # and no reading of the real history
        rows = []
        week_scale = {}
        for h in _working(monday - WEEKS * 7 * 86400, monday):
            usage = _usage(rng, week_scale.setdefault((monday - h) // (7 * 86400), rng.uniform(0.8, 1.2)))
            rows.append(dict(ts=h + 600, session_id="demo", platform="demo", provider="demo", model=BIG, aux_task="",
                             reasoning_tokens=0, cost_usd=o.prices.cost(BIG, usage)[0], cost_status="estimated", **usage))
        o.store.add_calls(rows)
        o.store.set_meta("first_call_at", rows[0]["ts"])
        usual = sum(r["cost_usd"] for r in rows) / WEEKS
        settings["budget_usd"] = limit = round(usual * 1.2)

        lines, model, without, seen = [], BIG, 0.0, None
        for h in _working(monday, sunday):
            sim["now"] = h
            d = datetime.fromtimestamp(h)
            if d.hour in CHECK_HOURS:
                if seen is None and o.outlook().state == "over":
                    seen = (o.desktop_snapshot(), h)
                new = o.llm_request(request={"model": BIG}, provider="demo", session_id="demo")
                before, model = model, new["request"]["model"] if new else BIG
                if d.hour == 12 or model != before:
                    move = ""
                    if model != before:
                        move = "  → switches to the cheaper model" if model == SMALL else "  → back to the usual model"
                    lines.append(f"  {d:%a %H:%M}  {_budget.line(o.outlook(), h)}{move}")
            usage = _usage(rng, HEAVY)
            without += o.prices.cost(BIG, usage)[0]
            o.store.add_call(ts=h + 600, session_id="demo", provider="demo", model=model,
                             cost_usd=o.prices.cost(model, usage)[0], cost_status="estimated", **usage)
        sim["now"] = sunday - 60
        final = o.store.spent(monday, sunday)
    head = [f"Obol demo · made-up usage, nothing saved. Limit ${limit} a week, a usual week is about ${usual:.0f}, "
            f"this one is {HEAVY:g}× as heavy.", "  forecast: from the usage so far"]
    tail = [f"  With no plugin: ${without:.2f}, ${without - limit:.2f} over the limit.",
            f"  With Obol in save mode: ${final:.2f}."]
    chart = []
    if seen:
        if show:
            show(_chip(seen[0]))
        chart = [f"  What Obol saw on {datetime.fromtimestamp(seen[1]):%a %H:%M}, against the ${limit} limit:",
                 *_bars(seen[0]["chart"])]
    return "\n".join(head + chart + lines + tail)
