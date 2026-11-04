"""/obol: the slash command (CLI, gateway and Desktop), and the agent-facing tool schema."""
from __future__ import annotations

import time
from datetime import datetime

from . import _budget, _forecast

TOOL_SCHEMA = {
    "name": "obol_budget",
    "description": ("This agent's spend budget: spent so far, the forecast for the rest of the period, and when "
                    "it would run out at this pace. Check before starting long or expensive work."),
    "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
}

HELP = """/obol                         status and forecast
/obol budget 20 week [provider] set a budget (day, week or month; provider optional, e.g. openrouter)
/obol mode watch|steer|guard    watch: tell the agent · steer: also use the cheap model when the forecast
                                says the budget won't last · guard: also stop new turns once it's used up
/obol cheap <model id>          the cheaper model steer switches to (same provider)
/obol forecast                  the next hours, hour by hour
/obol models                    spend by model this period
/obol model install             download Cascade 4M to forecast locally (no key needed)
/obol forecaster auto|ephemeris|cascade|seasonal"""

SPARK = "▁▂▃▄▅▆▇█"


def _spark(xs):
    xs = [x or 0.0 for x in xs]
    top = max(xs) if xs else 0
    return "".join(SPARK[min(7, int(7 * x / top))] if top else SPARK[0] for x in xs)


def run(obol, raw_args: str = "") -> str:
    args = (raw_args or "").split()
    cmd = args[0].lower() if args else ""
    try:
        if cmd in ("", "status"):
            return _status(obol)
        if cmd in ("help", "-h", "--help"):
            return HELP
        if cmd == "budget":
            if len(args) < 2:
                return "Usage: /obol budget <usd> [day|week|month] [provider]"
            usd = float(args[1].lstrip("$"))
            obol._set("budget_usd", usd)
            if len(args) > 2:
                if args[2] not in _budget.PERIODS:
                    return f"Period must be one of {', '.join(_budget.PERIODS)}."
                obol._set("period", args[2])
            if len(args) > 3:
                obol._set("provider", "" if args[3] in ("all", "*") else args[3])
            return "Budget saved.\n" + _status(obol)
        if cmd == "mode":
            if len(args) < 2 or args[1] not in _budget.MODES:
                return "Usage: /obol mode watch|steer|guard"
            obol._set("mode", args[1])
            b = obol.budget()
            warn = "" if args[1] != "steer" or b.cheap_model else "\nSet the model to switch to: /obol cheap <model id>"
            return f"Mode: {args[1]}.{warn}"
        if cmd == "cheap":
            if len(args) < 2:
                return "Usage: /obol cheap <model id>  (a model on the same provider, e.g. openai/gpt-5-mini)"
            obol._set("cheap_model", args[1])
            return f"Steer will switch to {args[1]}."
        if cmd == "forecaster":
            if len(args) < 2 or args[1] not in ("auto", "ephemeris", "cascade", "seasonal"):
                return "Usage: /obol forecaster auto|ephemeris|cascade|seasonal"
            obol._set("forecaster", args[1])
            return f"Forecaster: {args[1]}."
        if cmd == "forecast":
            return _hours(obol)
        if cmd == "models":
            return _models(obol)
        if cmd == "model" and args[1:2] == ["install"]:
            return _forecast.cascade_install(obol.data_dir)
        return HELP
    except ValueError:
        return "That isn't a number. " + HELP.splitlines()[1]


def _status(obol) -> str:
    s = obol.status()
    b = s["budget"]
    if "state" not in s:
        return f"Obol · {s['line']}"
    lines = [f"Obol · {s['line']}",
             f"  {_budget.ADJ[b['period']]} budget{' on ' + b['provider'] if b['provider'] else ''} · mode {b['mode']}"
             + (f" → {b['cheap_model']}" if b['mode'] == 'steer' and b['cheap_model'] else "")]
    if s.get("forecaster"):
        lines.append(f"  forecast: {s['model']}" + (f" ({s['forecaster_note']})" if s.get("forecaster_note") else ""))
    hours = obol.store.hourly(provider=b["provider"])[-48:]
    if hours:
        lines.append(f"  last {len(hours)}h  {_spark([v for _, v in hours])}")
    if s.get("unpriced_requests"):
        lines.append(f"  {s['unpriced_requests']} requests had no price, so spend is a lower bound")
    if st := s.get("last_steer"):
        lines.append(f"  last steer {datetime.fromtimestamp(st['at']):%a %H:%M}: {st['from']} → {st['to']}")
    if err := s.get("last_error"):
        lines.append(f"  {err['tier']} unavailable ({err['error'][:80]}); using the local forecast")
    return "\n".join(lines)


def _hours(obol) -> str:
    b = obol.budget()
    f = obol.forecast(b)
    if not f:
        return "No spend recorded yet: Obol starts forecasting after the first request."
    now = time.time()
    q = f["quantiles"]
    rows = [f"Forecast by {f['model']} (per-hour ranges are nominal 10–90%):"]
    for i in range(min(12, len(q["0.5"]))):
        h = f["start"] + i * 3600
        if h + 3600 <= now:
            continue
        lo, md, hi = (q[k][i] for k in _forecast.LEVELS)
        rows.append(f"  {datetime.fromtimestamp(h):%a %H:00}  ${md:7.3f}  (${lo:.3f}–${hi:.3f})")
    if b.active:
        rows.append(_budget.line(obol.outlook(b), now))
    return "\n".join(rows)


def _models(obol) -> str:
    b = obol.budget()
    start, _ = _budget.period_bounds(b.period)
    rows = obol.store.by_model(start, b.provider)
    if not rows:
        return "No requests this period."
    return "\n".join([f"Spend by model this {b.period}:"] +
                     [f"  ${usd:8.3f}  {n:5d} req  {model or '(unknown)'}" for model, n, usd in rows])
