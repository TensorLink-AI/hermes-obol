"""Declarative rules: when a condition holds, Obol takes an action. Data only: nothing is evaluated as code.

    rules:
      - name: unusual day
        when: {day_vs_usual: ">3x"}
        do: notify
      - when: {state: [tight, over], provider: openrouter}
        do: {steer: [anthropic/claude-sonnet-5.5, deepseek/deepseek-v4-flash]}   # a ladder: harsher state, cheaper rung
      - when: {request_usd: ">0.50"}
        do: {ask: "This request is large."}
      - when: {state: out}
        do: block

Conditions (all must hold): state, runs_out_within ("2h", "45m", "1d"), spent_pct (">=80"), request_usd,
day_vs_usual (">3x"), provider, model, platform (globs allowed), aux (true for background calls).
Actions: note (tell the agent), notify (tell the user, appended to the next reply), steer (model id, or a
ladder of them), ask (approval before tools; for a turn, it pauses until `/obol allow`), block.
Several rules can match; block wins over ask, and steer takes the first matching rule.
With no `rules` setting, the `mode` setting (alert, save, stop) is a preset of these rules.
"""
from __future__ import annotations

import fnmatch
import re

STATES = ("ok", "tight", "over", "out")
TASK_CAP = {"name": "task cap", "when": {"task_cap": True}, "do": {"block": True}}    # applies in every mode
SEVERITY = {"ok": 0, "unknown": 0, "tight": 1, "over": 2, "out": 3}
ACTIONS = ("note", "notify", "steer", "ask", "block")
_CMP = re.compile(r"^\s*(>=|<=|>|<|==|=)?\s*\$?\s*([0-9]*\.?[0-9]+(?:[eE][-+]?[0-9]+)?)\s*(x|%)?\s*$")
_DUR = re.compile(r"^\s*([0-9]*\.?[0-9]+)\s*(m|min|h|d)?\s*$")


def duration(text) -> float | None:
    m = _DUR.match(str(text))
    if not m:
        return None
    return float(m.group(1)) * {"m": 60, "min": 60, "h": 3600, "d": 86400, None: 3600}[m.group(2)]


def compare(value, spec) -> bool:
    if value is None:
        return False
    if isinstance(spec, (int, float)) and not isinstance(spec, bool):
        return value >= spec
    m = _CMP.match(str(spec))
    if not m:
        return False
    op, num = m.group(1) or ">=", float(m.group(2))
    return {">=": value >= num, "<=": value <= num, ">": value > num, "<": value < num,
            "==": value == num, "=": value == num}[op]


def _any(value, spec) -> bool:
    options = spec if isinstance(spec, list) else [spec]
    return any(fnmatch.fnmatch(str(value or ""), str(o)) for o in options)


def matches(when: dict, ctx: dict) -> bool:
    for key, spec in (when or {}).items():
        if key == "state":
            if not _any(ctx.get("state"), spec):
                return False
        elif key == "runs_out_within":
            limit, left = duration(spec), ctx.get("runs_out_in")
            if limit is None or left is None or left > limit:
                return False
        elif key in ("spent_pct", "request_usd", "day_vs_usual"):
            if not compare(ctx.get(key), spec):
                return False
        elif key in ("provider", "model", "platform"):
            if not _any(ctx.get(key), spec):
                return False
        elif key == "task_cap":
            if bool(spec) != bool(ctx.get("task_cap_hit")):
                return False
        elif key == "aux":
            if bool(spec) != bool(ctx.get("aux")):
                return False
        else:
            return False                     # an unknown condition never matches (fail safe, and visible in /obol rules)
    return True


def normalize(rule) -> dict | None:
    """{"name", "when", "do": {action: value}} or None when the rule is malformed."""
    if not isinstance(rule, dict):
        return None
    do = rule.get("do")
    if isinstance(do, str):
        do = {do: True}
    if not isinstance(do, dict) or not do or any(k not in ACTIONS for k in do):
        return None
    when = rule.get("when") or {}
    if not isinstance(when, dict):
        return None
    return {"name": str(rule.get("name") or ", ".join(do)), "when": when, "do": do}


def presets(mode: str, cheap_model: str, unusual_x: float, max_request_usd: float) -> list:
    ladder = [m.strip() for m in (cheap_model or "").split(",") if m.strip()]
    rules = [{"name": "budget note", "when": {"state": ["tight", "over", "out"]}, "do": {"note": True}},
             {"name": "budget alert", "when": {"state": ["tight", "over", "out"]}, "do": {"notify": True}}]
    if unusual_x and unusual_x > 0:
        rules.append({"name": "unusual day", "when": {"day_vs_usual": f">{unusual_x:g}x"}, "do": {"notify": True}})
    if mode == "save" and ladder:
        rules.append({"name": "save", "when": {"state": ["tight", "over", "out"], "aux": False},
                      "do": {"steer": ladder if len(ladder) > 1 else ladder[0]}})
    if max_request_usd and max_request_usd > 0:
        rules.append({"name": "large request", "when": {"request_usd": f">{max_request_usd:g}"},
                      "do": {"ask": True}})
    if mode == "stop":
        rules.append({"name": "stop", "when": {"state": "out"}, "do": {"block": True}})
    return rules


def evaluate(rules: list, ctx: dict) -> list:
    """[(rule, action, value)] for every matching rule's actions, in rule order."""
    out = []
    for r in rules:
        if matches(r["when"], ctx):
            for action, value in r["do"].items():
                out.append((r, action, value))
    return out


def steer_target(value, state: str) -> str | None:
    if isinstance(value, list):
        if not value:
            return None
        return str(value[min(max(SEVERITY.get(state, 1), 1), len(value)) - 1])
    return str(value) if value else None
