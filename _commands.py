"""/obol: the slash command (CLI, gateway and Desktop), and the agent-facing tool schema."""
from __future__ import annotations

import time
from datetime import datetime

from . import _budget, _forecast, _rules

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
/obol forecaster auto|ephemeris|cascade|seasonal
/obol allow +5                  add $5 to this period's budget
/obol allow                     let the next blocked or held turn through, once
/obol pause 1h | pause off      stop acting (metering continues) for a while
/obol cap 2 | cap off           cap this task: from now on, at most $2 more
/obol rules                     the rules in force (set your own under the `rules` setting)
/obol why                       what Obol did lately, and why"""

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
            obol.store.set_meta("steer_failed", None)
            return f"Steer will switch to {args[1]}."
        if cmd == "forecaster":
            if len(args) < 2 or args[1] not in ("auto", "ephemeris", "cascade", "seasonal"):
                return "Usage: /obol forecaster auto|ephemeris|cascade|seasonal"
            obol._set("forecaster", args[1])
            return f"Forecaster: {args[1]}."
        if cmd == "allow":
            if len(args) > 1:
                amount = float(args[1].lstrip("+$"))
                b = obol.budget()
                start, _ = _budget.period_bounds(b.period, obol.clock())
                key = f"topup:{b.period}:{int(start)}"
                obol.store.set_meta(key, float(obol.store.meta(key) or 0) + amount)
                obol._log("top-up", "you", f"+${amount:g} this {b.period}")
                return f"Added ${amount:g} to this {b.period}'s budget.\n" + _status(obol)
            obol.store.set_meta("allow_once", obol.clock())
            return "The next held or blocked turn will go through once."
        if cmd == "pause":
            if args[1:2] in (["off"], ["resume"]):
                obol.store.set_meta("paused_until", None)
                return "Obol is acting again."
            secs = _rules.duration(args[1] if len(args) > 1 else "1h")
            if not secs:
                return "Usage: /obol pause 30m | 2h | 1d | off"
            obol.store.set_meta("paused_until", obol.clock() + secs)
            obol._log("pause", "you", f"for {args[1] if len(args) > 1 else '1h'}")
            return (f"Paused until {datetime.fromtimestamp(obol.clock() + secs):%a %H:%M}: no notes, steering or "
                    "blocking. Spend is still metered.")
        if cmd == "cap":
            if args[1:2] == ["off"] or len(args) < 2:
                obol.store.set_meta("job_cap", None)
                return "Task cap removed."
            usd = float(args[1].lstrip("$"))
            obol.store.set_meta("job_cap", {"usd": usd, "since": obol.clock()})
            obol._log("cap", "you", f"${usd:g} from now")
            return f"Task cap set: at most ${usd:g} more from now. /obol cap off removes it."
        if cmd == "rules":
            lines = ["Rules in force" + (" (your `rules` setting):" if obol._get("rules", []) else
                                         f" (preset for mode {obol.budget().mode}):")]
            for r in obol.rules():
                when = ", ".join(f"{k} {v}" for k, v in r["when"].items()) or "always"
                do = ", ".join(k if v is True else f"{k} {v}" for k, v in r["do"].items())
                lines.append(f"  {r['name']}: when {when} → {do}")
            raw = obol._get("rules", []) or []
            bad = len(raw) - len(obol.rules()) if raw else 0
            if bad:
                lines.append(f"  ({bad} rule(s) in your setting are malformed and ignored)")
            return "\n".join(lines)
        if cmd == "why":
            acts = obol.store.meta("actions") or []
            if not acts:
                return "Obol hasn't acted yet."
            return "\n".join(["What Obol did lately:"] + [
                f"  {datetime.fromtimestamp(a['at']):%a %H:%M}  {a['action']:<10} ({a['rule']}) {a['detail']}"
                for a in acts[-12:]])
        if cmd == "forecast":
            return _hours(obol)
        if cmd == "models":
            return _models(obol)
        return HELP
    except ValueError:
        return "That isn't a number. " + HELP.splitlines()[1]


def _status(obol) -> str:
    s = obol.status()
    b = s["budget"]
    if "state" not in s:
        lr = s.get("learned")
        extra = (f"\n  learned from {lr['sessions']} past Hermes sessions; set a budget to see the forecast"
                 if lr and lr.get("sessions") else "")
        return f"Obol · {s['line']}{extra}"
    lines = [f"Obol · {s['line']}",
             f"  {_budget.ADJ[b['period']]} budget{' on ' + b['provider'] if b['provider'] else ''} · mode {b['mode']}"
             + (f" → {b['cheap_model']}" if b['mode'] == 'steer' and b['cheap_model'] else "")]
    if s.get("forecaster"):
        lines.append(f"  forecast: {s['model']}" + (f" ({s['forecaster_note']})" if s.get("forecaster_note") else ""))
    hours = obol.store.hourly(provider=b["provider"])[-48:]
    if hours:
        lines.append(f"  last {len(hours)}h  {_spark([v for _, v in hours])}")
    if (pu := obol.store.meta("paused_until")) and pu > obol.clock():
        lines.append(f"  paused until {datetime.fromtimestamp(pu):%a %H:%M} (/obol pause off)")
    if job := obol.store.meta("job_cap"):
        used = obol.store.spent(job["since"], obol.clock(), "")
        lines.append(f"  task cap: ${used:.2f} of ${job['usd']:g} since {datetime.fromtimestamp(job['since']):%H:%M}")
    if lr := s.get("learned"):
        lines.append(f"  learned from {lr['sessions']} past Hermes sessions since "
                     f"{datetime.fromtimestamp(lr['since']):%d %b}")
    if s.get("unpriced_requests"):
        lines.append(f"  {s['unpriced_requests']} requests had no price, so spend is a lower bound")
    if st := s.get("last_steer"):
        lines.append(f"  last steer {datetime.fromtimestamp(st['at']):%a %H:%M}: {st['from']} → {st['to']}")
    if bad := s.get("steer_paused"):
        lines.append(f"  steer paused: the provider refused {bad['model']} ({bad['error'][:80]})")
    if s.get("forecaster") in ("cascade", "seasonal") and not s.get("ephemeris_key"):
        lines.append("  optional: Ephemeris runs a panel of larger forecasting models (Chronos-2, TimesFM, Toto…);"
                     " add a key under Settings → Plugins → Obol · ephemeris.cascade.industries")
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
