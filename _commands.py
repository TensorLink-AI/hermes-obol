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

HELP = """/obol                     how you're doing
/obol budget 20 week      set a limit (day, week or month)
/obol mode alert|save|stop
    alert: tell you and the agent when the budget won't last (default)
    save:  also switch to a cheaper model when it won't last (set it with /obol cheap <model>)
    stop:  also stop new turns once it's used up
/obol topup 5             add $5 to this period
/obol pause 2h            take a break from Obol acting (spend is still recorded)
/obol more                everything else"""

MORE = """/obol cheap <model>       the cheaper model save mode switches to (same provider)
/obol allow               let one held or stopped turn through
/obol cap 2 | cap off     cap the current task at $2 more
/obol providers           spend, forecast and price per API
/obol budget 10 week openrouter   a budget for one API (0 removes it); /obol topup 5 openrouter
/obol forecast            the next hours, hour by hour
/obol models              spend by model this period
/obol why                 what Obol did lately, and why
/obol rules               the rules in force (write your own in the `rules` setting)
/obol forecaster auto|cascade|ephemeris|seasonal
/obol pause off           act again"""

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
        if cmd in ("more", "advanced"):
            return MORE
        if cmd == "budget":
            if len(args) < 2:
                return "Usage: /obol budget <usd> [day|week|month] [provider]"
            usd = 0.0 if args[1] in ("off", "none") else float(args[1].lstrip("$"))
            period = args[2] if len(args) > 2 else None
            if period and period not in _budget.PERIODS:
                return f"Period must be one of {', '.join(_budget.PERIODS)}."
            provider = args[3] if len(args) > 3 and args[3] not in ("all", "*") else ""
            if provider:                                    # a budget for one API, beside the main one
                obol.set_provider_budget(provider, usd, period or obol.budget().period)
                done = f"Budget for {provider} removed." if usd <= 0 else f"Budget for {provider} saved."
            else:
                obol._set("budget_usd", usd)
                if period:
                    obol._set("period", period)
                done = "Budget removed." if usd <= 0 else "Budget saved."
            return done + "\n" + _status(obol)
        if cmd == "mode":
            mode = _budget.LEGACY.get(args[1], args[1]) if len(args) > 1 else ""
            if mode not in _budget.MODES:
                return "Usage: /obol mode alert|save|stop\n" + "\n".join(
                    f"  {m}: {_budget.MODE_HELP[m]}" for m in _budget.MODES)
            obol._set("mode", mode)
            warn = ("" if mode != "save" or obol.budget().cheap_model
                    else "\nTell it which cheaper model to use: /obol cheap <model id>")
            return f"Mode: {mode}. Obol {_budget.MODE_HELP[mode]}.{warn}"
        if cmd == "cheap":
            if len(args) < 2:
                return "Usage: /obol cheap <model id>  (a model on the same provider, e.g. openai/gpt-5-mini)"
            obol._set("cheap_model", args[1])
            obol.store.set_meta("steer_failed", None)
            return f"Save mode will switch to {args[1]} when the budget won't last."
        if cmd == "forecaster":
            if len(args) < 2 or args[1] not in ("auto", "ephemeris", "cascade", "seasonal"):
                return "Usage: /obol forecaster auto|ephemeris|cascade|seasonal"
            obol._set("forecaster", args[1])
            return f"Forecaster: {args[1]}."
        if cmd == "topup" or (cmd == "allow" and len(args) > 1):
            if len(args) < 2:
                return "Usage: /obol topup 5"
            amount = float(args[1].lstrip("+$"))
            provider = args[2] if len(args) > 2 else ""
            b = obol.topup(amount, provider)
            if b is None:
                return f"No budget{' for ' + provider if provider else ''} to top up: /obol budget 20 week"
            return f"Added ${amount:g} to this {b.period} ({obol.label(b)}).\n" + _status(obol)
        if cmd == "allow":
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
            return (f"Paused until {datetime.fromtimestamp(obol.clock() + secs):%a %H:%M}: no alerts, model switches "
                    "or stops. Spend is still recorded. /obol pause off to resume.")
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
        if cmd in ("providers", "apis"):
            return _providers(obol)
        if cmd == "forecast":
            return _hours(obol)
        if cmd == "models":
            return _models(obol)
        return f"Unknown: /obol {cmd}\n" + HELP
    except ValueError:
        return "That isn't a number, e.g. /obol budget 20 week"


def _status(obol) -> str:
    s = obol.status()
    b = s["budget"]
    if "state" not in s:
        return _no_budget(obol, s)
    lines = [f"Obol · {s['line']}",
             f"  {_budget.ADJ[b['period']]} budget{' on ' + b['provider'] if b['provider'] else ''} · {b['mode']} mode"
             + (f" (switches to {b['cheap_model']})" if b['mode'] == 'save' and b['cheap_model'] else "")]
    if len(s.get("budgets") or []) > 1:
        for r in s["budgets"]:
            lines.append(f"  {r['label']}: {r['line']}")
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
        lines.append(f"  last switch {datetime.fromtimestamp(st['at']):%a %H:%M}: {st['from']} → {st['to']}")
    if bad := s.get("steer_paused"):
        lines.append(f"  not switching for now: the provider refused {bad['model']} ({bad['error'][:80]})")
    if s.get("forecaster") in ("cascade", "seasonal") and not s.get("ephemeris_key"):
        lines.append("  optional: larger hosted forecasting models via Ephemeris (key in Settings → Plugins → Obol)")
    if err := s.get("last_error"):
        lines.append(f"  {err['tier']} unavailable ({err['error'][:80]}); using the local forecast")
    return "\n".join(lines)


def _nice(x: float) -> float:
    """Round a suggested budget up to a number people would pick."""
    import math
    for step in (0.05, 0.1, 0.5, 1, 5, 10, 25, 50, 100):
        if x <= step * 20:
            return round(math.ceil(x / step) * step, 2)
    return float(math.ceil(x / 100) * 100)


def _no_budget(obol, s) -> str:
    """Useful before any setup: what you spend, what's usual, where this week is heading, and a budget to try."""
    u = obol.unbudgeted()
    lines = ["Obol · no budget yet"]
    lr = s.get("learned")
    if not u.get("spent") and not u.get("usual"):
        lines.append("  no spend recorded yet: Obol starts learning with your first request")
        return "\n".join(lines)
    per = u["period"]
    lines.append(f"  this {per}: {_budget._money(u['spent'])} so far"
                 + (f", on track for {_budget._money(u['expected'])}" if u.get("expected") is not None else ""))
    if u.get("usual"):
        lines.append(f"  a usual {per}: about {_budget._money(u['usual'])}")
        suggest = _nice(max(u["usual"], u.get("expected") or 0) * 1.2)
        lines.append(f"  try: /obol budget {suggest:g} {per}   (20% above that)")
    elif u.get("expected"):
        lines.append(f"  try: /obol budget {_nice(u['expected'] * 1.2):g} {per}   (20% above where it's heading)")
    else:
        lines.append(f"  try: /obol budget 20 {per}")
    if lr and lr.get("sessions"):
        lines.append(f"  learned from {lr['sessions']} past Hermes sessions")
    return "\n".join(lines)


def _providers(obol) -> str:
    rows = obol.providers()
    if not rows:
        return "No requests in the last 30 days."
    per = rows[0]["period"]
    out = [f"By provider, this {per} (each forecast on its own usage, at its own price):"]
    for r in rows:
        price = f"${r['usd_per_mtok']:.2f}/M tok" if r["usd_per_mtok"] else "unpriced"
        exp = f" → {_budget._money(r['expected'])}" if r["expected"] is not None else ""
        tail = f"   budget: {r['budget']['line']}" if r["budget"] else ""
        out.append(f"  {r['provider']:<14} {_budget._money(r['spent'])}{exp:<12} {price:<14}{tail}")
    out.append("  set one: /obol budget 10 week <provider>")
    return "\n".join(out)


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
