"""/obol: the slash command (CLI, gateway and Desktop), and the agent-facing tool schema."""
from __future__ import annotations

import math
from datetime import datetime

from . import _budget, _demo, _forecast, _limits, _rules

_SECRET = "EPHEMERIS_API_KEY"

TOOL_SCHEMA = {
    "name": "obol_budget",
    "description": ("This agent's spend budget: spent so far, the forecast for the rest of the period, and when "
                    "the forecast says it runs out. Check before starting long or expensive work."),
    "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
}

HELP = """/obol                     how you're doing
/obol budget 20 week      set a limit in US dollars (day, week from Monday, or month; off removes it)
/obol mode alert|save|stop
    alert: tell you and the agent once the forecast reaches 85% of the limit (default)
    save:  also switch to a cheaper model at that point (set it with /obol cheap <model>)
    stop:  also stop the agent once the budget is used up
/obol limits              Claude and Codex plan limits: will they last until they reset?
/obol demo                see what Obol does, on a made-up week (nothing saved)
/obol why                 what this is, and what Obol did lately
/obol pause 2h            take a break from Obol acting (spend is still recorded)
/obol more                everything else"""

MORE = """/obol cheap <model>|off   the cheaper model save mode switches to (same provider)
/obol topup 5             add $5 to this period (/obol allow 5 does the same)
/obol allow               let your next stopped message through, once
/obol cap 2 | cap off     stop after $2 more, in every session, until you remove it (/obol cap shows it)
/obol providers           spend, forecast and price per API
/obol budget 10 week openrouter   a budget for one API (0 removes it); /obol topup 5 openrouter
/obol forecast            the next hours, hour by hour
/obol models              spend by model this period
/obol rules               the rules in force (write your own in the `rules` setting)
/obol forecaster auto|cascade|ephemeris|seasonal
/obol pause | pause off   an hour's break; act again"""

EXPLAIN = ("Obol is a plugin that watches how many tokens Hermes uses, forecasts when your budget or plan limit will run "
           "out, and then warns you, switches to a cheaper model or stops, depending on /obol mode.")

SPARK = "▁▂▃▄▅▆▇█"


def _spark(xs):
    xs = [x or 0.0 for x in xs]
    top = max(xs) if xs else 0
    return "".join(SPARK[min(7, int(7 * x / top))] if top else SPARK[0] for x in xs)


def _show(v) -> str:
    return ", ".join(str(x) for x in v) if isinstance(v, list) else str(v)


def _amount(text: str) -> float:
    """A positive, finite amount typed by the user; ValueError otherwise (`nan` and `inf` parse as floats)."""
    x = float(text.lstrip("+$"))
    if not math.isfinite(x) or x <= 0:
        raise ValueError(text)
    return x


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
            usd = 0.0 if args[1] in ("off", "none", "0") else _amount(args[1])
            period = args[2] if len(args) > 2 else None
            if period and period not in _budget.PERIODS:
                return f"Period must be one of {', '.join(_budget.PERIODS)}."
            provider = args[3].lower() if len(args) > 3 and args[3] not in ("all", "*") else ""
            if provider:                                    # a budget for one API, beside the main one
                obol.set_provider_budget(provider, usd, period or obol.budget().period)
                done = f"Budget for {provider} removed." if usd <= 0 else f"Budget for {provider} saved."
            else:
                obol._set("budget_usd", usd)
                if period:
                    obol._set("period", period)
                done = "Budget removed." if usd <= 0 else "Budget saved."
            return done + ("" if usd <= 0 else _plan_note(obol, provider) + _budget_note(obol, provider, period)) + "\n" + _status(obol)
        if cmd == "mode":
            mode = _budget.LEGACY.get(args[1], args[1]) if len(args) > 1 else ""
            if mode not in _budget.MODES:
                return "Usage: /obol mode alert|save|stop\n" + "\n".join(
                    f"  {m}: {_budget.MODE_HELP[m]}" for m in _budget.MODES)
            obol._set("mode", mode)
            warn = ("" if mode != "save" or obol.budget().cheap_model
                    else "\nTell it which cheaper model to use: /obol cheap <model id>")
            if obol._get("rules", []):
                warn += "\nYour `rules` setting replaces the modes, so this won't change anything until you clear it."
            elif mode != "alert" and not obol.budgets() and not any((obol.store.meta(f"limits:{p}") or {}).get("windows") for p in _limits.PROVIDERS):
                warn += "\nThere's no budget yet, so nothing can trigger it: /obol budget 20 week"
            return f"Mode: {mode}. Obol {_budget.MODE_HELP[mode]}.{warn}"
        if cmd == "cheap":
            if len(args) < 2:
                return "Usage: /obol cheap <model id>  (a model on the same provider, e.g. openai/gpt-5-mini)"
            if args[1].lower() in ("off", "none"):
                obol._set("cheap_model", "")
                return "Cheaper model cleared. Save mode won't switch until you pick one."
            obol._set("cheap_model", args[1])
            obol.store.set_meta("steer_failed", None)
            where = obol.store.latest_provider()
            warn = ("" if not where or obol._can_steer(args[1].split(",")[0].strip(), "", where)
                    else f"\n⚠ Obol can't switch to this on {where}, so nothing will change while you use it.")
            if obol.budget().mode != "save" and not obol._get("rules", []):
                warn += "\nNothing switches until you turn it on: /obol mode save"
            return f"Save mode will switch to {args[1]} once the forecast reaches 85% of the limit.{warn}"
        if cmd == "forecaster":
            if len(args) < 2 or args[1] not in ("auto", "ephemeris", "cascade", "seasonal"):
                return "Usage: /obol forecaster auto|ephemeris|cascade|seasonal"
            obol._set("forecaster", args[1])
            if args[1] != "ephemeris":
                return f"Forecaster: {args[1]}."
            return ("Forecaster: ephemeris. Your hourly dollar totals go to Ephemeris, and each forecast spends its credits."
                    + ("" if obol._secret(_SECRET) else " Save a key first, in Settings → Plugins → Obol."))
        if cmd == "topup" or (cmd == "allow" and len(args) > 1):
            if len(args) < 2:
                return "Usage: /obol topup 5"
            amount = _amount(args[1])
            provider = args[2].lower() if len(args) > 2 else ""
            b = obol.topup(amount, provider)
            if b is None:
                return f"No budget{' for ' + provider if provider else ''} to top up: /obol budget 20 week"
            when = "today's" if b.period == "day" else f"this {b.period}'s"
            name = f"{b.provider} budget" if b.provider else "budget"
            return f"Added ${amount:g} to {when} {name}.\n" + _status(obol)
        if cmd == "allow":
            obol.store.set_meta("allow_once", obol.clock())
            return ("Your next stopped message in the next hour goes through, tools included. Just that one: after it, "
                    "messages stop again. Send your message now.")
        if cmd == "pause":
            if args[1:2] in (["off"], ["resume"]):
                was = (obol.store.meta("paused_until") or 0) > obol.clock()
                obol.store.set_meta("paused_until", None)
                return "Obol is acting again." if was else "Obol wasn't paused."
            secs = _rules.duration(args[1] if len(args) > 1 else "1h")
            if not secs:
                return "Usage: /obol pause 30m | 2h | 1d | off"
            obol.store.set_meta("paused_until", obol.clock() + secs)
            obol._log("pause", "you", f"for {args[1] if len(args) > 1 else '1h'}")
            return (f"Paused until {datetime.fromtimestamp(obol.clock() + secs):%a %H:%M}: no alerts, model switches "
                    "or stops. Spend is still recorded. /obol pause off to resume.")
        if cmd == "cap":
            job = obol.store.meta("job_cap")
            if len(args) < 2:
                if not job:
                    return "No spending cap set. /obol cap 2 stops after $2 more."
                used = obol.store.spent(job["since"], obol.clock(), "")
                return f"Spending cap: ${used:.2f} of ${job['usd']:g} used since it was set. /obol cap off removes it."
            if args[1] == "off":
                obol.store.set_meta("job_cap", None)
                return "Spending cap removed." if job else "No spending cap was set."
            usd = _amount(args[1])
            obol.store.set_meta("job_cap", {"usd": usd, "since": obol.clock()})
            obol._log("cap", "you", f"${usd:g} from now")
            return (f"Spending cap set: at most ${usd:g} more from now, across every session, then Obol stops turns until "
                    "you remove it. It counts API spend only, not plan usage. /obol cap off removes it.")
        if cmd == "rules":
            lines = ["Rules in force" + (" (your `rules` setting):" if obol._get("rules", []) else
                                         f" (preset for mode {obol.budget().mode}):")]
            for r in obol.rules():
                when = "; ".join(f"{k} {_show(v)}" for k, v in r["when"].items()) or "always"
                do = "; ".join(k if v is True else f"{k} {_show(v)}" for k, v in r["do"].items())
                lines.append(f"  {r['name']}: when {when} → {do}")
                lines += [f"    ⚠ this rule can never fire: {p}" for p in _rules.problems(r["when"])]
            raw = obol._get("rules", []) or []
            lines += [f"  ⚠ rule {i} is ignored: {_rules.why_ignored(x)}" for i, x in enumerate(raw, 1) if not _rules.normalize(x)]
            lines.append("  and always: a spending cap, if you've set one with /obol cap")
            return "\n".join(lines)
        if cmd == "why":
            acts = obol.store.meta("actions") or []
            if not acts:
                return f"{EXPLAIN}\nIt hasn't had to do anything yet."
            return "\n".join([EXPLAIN, "What it did lately:"] + [
                f"  {datetime.fromtimestamp(a['at']):%a %H:%M}  {a['action']:<10} ({a['rule']}) {a['detail']}"
                for a in acts[-12:]])
        if cmd in ("limits", "limit", "plan"):
            return _limits_view(obol)
        if cmd == "demo":
            return _demo.run(type(obol), obol.clock(), obol.show_demo)
        if cmd in ("providers", "apis"):
            return _providers(obol)
        if cmd == "forecast":
            return _hours(obol)
        if cmd == "models":
            return _models(obol)
        return f"Unknown: /obol {cmd}\n" + HELP
    except ValueError:
        return "That needs an amount above zero, e.g. /obol budget 20 week"


def _budget_note(obol, provider: str, period: str | None) -> str:
    """What surprises people about a new budget: it already has spend in it, when it resets, and whose dollars they are."""
    b = obol.budget()
    period = period or b.period
    start, _ = _budget.period_bounds(period, obol.clock())
    spent = obol.store.spent(start, obol.clock(), provider or b.provider)
    first = {"week": "Weeks start on Monday. ", "month": "Months start on the 1st. "}.get(period, "")
    return ((f"\n{_budget._money(spent)} of Hermes use this {period} already counts toward it." if spent > 0 else "")
            + f"\n{first}Amounts are US dollars at list price, so your provider's bill can differ.")


def _history_line(s) -> list:
    return ["  couldn't read your past Hermes usage yet, so forecasts start from now. Obol will try again."] if s.get("history_failed") else []


def _plan_note(obol, provider: str) -> str:
    p = provider or obol.store.latest_provider()
    if not obol.on_plan(p):
        return ""
    return f"\nYou're on a {_limits.NAMES[p]} plan, so this counts API-key use only. Plan limits need no budget: /obol limits"


def _status(obol) -> str:
    obol.read_limits_if_unknown()
    s = obol.status()
    b = s["budget"]
    if "state" not in s:
        return _no_budget(obol, s)
    lines = [f"Obol · {s['line']}",
             f"  {_budget.ADJ[b['period']]} budget{' on ' + b['provider'] if b['provider'] else ''}, "
             f"resets {_budget._when(s['period_end'], obol.clock())} · "
             + ("your own rules" if obol._get("rules", []) else f"{b['mode']} mode"
                + (f" (switches to {b['cheap_model']})" if b['mode'] == 'save' and b['cheap_model'] else ""))]
    if what := obol.what_if(_budget.Budget(**b)):
        verb = "switching" if b["mode"] == "save" else "with /obol mode save"
        lines.append(f"  what if: on {what['model']}, {what['text']}" + ("" if b["mode"] == "save" else f" ({verb})"))
    if b["mode"] == "save" and not b["cheap_model"]:
        lines.append("  ⚠ save mode can't switch yet: pick a model with /obol cheap <model>")
    lines += [f"  {w['line']}" for w in s["limits"][:3]]
    if len(s.get("budgets") or []) > 1:
        for r in s["budgets"]:
            lines.append(f"  {r['label']}: {r['line']}")
    if s.get("forecaster"):
        lines.append(f"  forecast: {s['model']}" + (f" ({s['forecaster_note']})" if s.get("forecaster_note") else "")
                     + (" · a rough guide, so plan for it being early or late" if s["state"] == "over" else ""))
    hours = obol.store.hourly(until=obol.clock(), provider=b["provider"])[-48:]
    if len({v for _, v in hours}) > 1:                 # a flat line says nothing
        lines.append(f"  last {len(hours)}h  {_spark([v for _, v in hours])}")
    if (pu := obol.store.meta("paused_until")) and pu > obol.clock():
        lines.append(f"  paused until {datetime.fromtimestamp(pu):%a %H:%M} (/obol pause off)")
    if job := obol.store.meta("job_cap"):
        used = obol.store.spent(job["since"], obol.clock(), "")
        lines.append(f"  spending cap: ${used:.2f} of ${job['usd']:g} since {datetime.fromtimestamp(job['since']):%H:%M}")
    if lr := s.get("learned"):
        lines.append(f"  learned from {lr['sessions']} past Hermes sessions since "
                     f"{datetime.fromtimestamp(lr['since']):%d %b}")
    if s.get("unpriced_requests"):
        lines.append(f"  {s['unpriced_requests']} requests had no price, so spend is a lower bound")
    lines += _switch_line(s) + _history_line(s)
    if bad := s.get("steer_paused"):
        lines.append(f"  not switching for now: the provider refused {bad['model']} ({bad['error'][:80]})")
    if (s.get("forecaster") in ("cascade", "seasonal") and not s.get("ephemeris_key") and s["state"] == "ok"
            and not obol.store.meta("ephemeris_hint_shown")):       # once, and never while you're short of budget
        obol.store.set_meta("ephemeris_hint_shown", obol.clock())
        lines.append("  optional: larger hosted forecasting models via Ephemeris (save a key in Settings → Plugins → Obol, "
                     "then /obol forecaster ephemeris)")
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
    plans = list(dict.fromkeys(_limits.NAMES[w["provider"]] for w in s["limits"]))
    lines = [f"Obol · {' and '.join(plans)} plan{'s' if len(plans) > 1 else ''}: nothing to set up" if plans
             else "Obol · no budget yet"]
    lines += [f"  {w['line']}" for w in s["limits"][:3]]
    lines += _failed_reads(obol) + _switch_line(s) + _history_line(s)
    lr = s.get("learned")
    if not u.get("spent") and not u.get("usual"):
        lines.append("  on API keys too? Set a dollar limit: /obol budget 20 week" if plans
                     else "  no spend yet. Set a limit now: /obol budget 20 week")
        lines.append("  see it work first: /obol demo")
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


def _switch_line(s) -> list:
    st = s.get("last_steer")
    if not st:
        return []
    at = f"{datetime.fromtimestamp(st['at']):%a %H:%M}"
    if s.get("switched_now"):
        return [f"  using {st['to']} now (switched from {st['from']} {at}); it goes back when the budget recovers"]
    return [f"  not switched now (last switch {at}: {st['from']} → {st['to']})"]


def _failed_reads(obol) -> list:
    """A read that failed mustn't pass for having no plan. An API key or no login isn't a failure, so those stay quiet:
    a provider only counts as failing if the read broke or it's the one requests are going to."""
    out, latest = [], obol.store.latest_provider()
    for p in _limits.PROVIDERS:
        rec = obol.store.meta(f"limits:{p}") or {}
        err = rec.get("error")
        if err and not rec.get("windows") and (err.startswith("couldn't") or (p == latest and "OAuth" not in err)):
            out.append(f"  {_limits.NAMES[p]} plan limits: {err}. Try /obol limits again shortly.")
    return out


def _limits_view(obol) -> str:
    """Reads now, so it's current whenever you ask."""
    if not obol._limits_on():
        return "Plan limits are off (the `plan_limits` setting)."
    lines, why = [], []
    for p in _limits.PROVIDERS:
        rec = obol.read_limits(p)
        rows = obol.limit_windows(p)
        lines += [f"  {w['line']}" for w in rows]
        if rec.get("error"):
            why.append(f"  {_limits.NAMES[p]}: {rec['error']}")
    if not lines and not why:
        return "No plan limits to show."
    return "\n".join((["Plan limits (the vendor's figures, across every client on the account):"] + lines if lines
                      else ["No plan limits to show."]) + why)


def _providers(obol) -> str:
    rows = obol.providers()
    if not rows:
        return "No requests in the last 30 days."
    per = rows[0]["period"]
    out = [f"By provider, this {per} (each forecast on its own usage, at its own price):"]
    for r in rows:
        price = "on a plan" if obol.on_plan(r["provider"]) else f"${r['usd_per_mtok']:.2f}/M tok" if r["usd_per_mtok"] else "unpriced"
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
    now = obol.clock()
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
    start, _ = _budget.period_bounds(b.period, obol.clock())
    rows = obol.store.by_model(start, b.provider)
    if not rows:
        return "No requests this period."
    return "\n".join([f"Spend by model this {b.period}:"] +
                     [f"  ${usd:8.3f}  {n:5d} req  {model or '(unknown)'}" for model, n, usd in rows])
