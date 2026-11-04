"""Obol plugin core.

Meters every provider request (main loop and auxiliary calls), forecasts hourly spend, and depending on
the budget's mode tells the agent (watch), switches to a cheaper model when the forecast says the budget
won't last (steer), or stops new turns and tools once it is used up (guard).
"""
from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path

from . import _budget, _forecast
from ._pricing import Prices
from ._store import HOUR, Store

log = logging.getLogger("hermes_plugins.obol")

SECRET = "EPHEMERIS_API_KEY"
LOCAL_REFRESH = 10 * 60          # the seasonal forecast is cheap: recompute this often
MODEL_REFRESH = HOUR             # local model / Ephemeris: at most hourly, and only after a new hour of data
FAIL_BACKOFF = 3 * HOUR          # after an Ephemeris failure, stay local this long


class Obol:
    def __init__(self, ctx=None, data_dir: Path | None = None, get=None, set_=None, secret=None, clock=time.time):
        self.ctx = ctx
        self._data_dir = Path(data_dir) if data_dir else None
        self._store = self._prices = None
        self._get = get or (lambda k, d=None: ctx.get_config(k, d))
        self._set = set_ or (lambda k, v: ctx.set_config(k, v))
        self._secret = secret or _read_secret
        self.clock = clock
        self._lock = threading.Lock()
        self._refreshing = False

    # opened on first use, not in register(): the plugin probe has no profile data directory
    @property
    def data_dir(self) -> Path:
        if self._data_dir is None:
            self._data_dir = Path(self.ctx.state.data_dir)
        return self._data_dir

    @property
    def store(self) -> Store:
        if self._store is None:
            self._store = Store(self.data_dir / "obol.db")
        return self._store

    @property
    def prices(self) -> Prices:
        if self._prices is None:
            self._prices = Prices(self.data_dir, overrides=lambda: self._get("prices", {}) or {})
        return self._prices

    # ── settings ─────────────────────────────────────────────────────────────
    def budget(self) -> _budget.Budget:
        return _budget.Budget.from_config(self._get)

    # ── meter ────────────────────────────────────────────────────────────────
    def meter(self, **kw):
        """post_api_request / post_auxiliary_call: record tokens and cost. Never raises, never blocks on network."""
        usage = kw.get("usage")
        if not isinstance(usage, dict):
            return
        model = kw.get("response_model") or kw.get("model") or ""
        cost, status = self.prices.cost(model, usage)
        if self.prices.stale(self.clock()):
            self.prices.refresh_async(on_done=lambda: self.store.reprice(self.prices.cost))
        self.store.add_call(
            session_id=kw.get("session_id") or "", platform=kw.get("platform") or "",
            provider=kw.get("provider") or "", model=model, aux_task=kw.get("aux_task") or "",
            input_tokens=usage.get("input_tokens"), output_tokens=usage.get("output_tokens"),
            cache_read_tokens=usage.get("cache_read_tokens"), cache_write_tokens=usage.get("cache_write_tokens"),
            reasoning_tokens=usage.get("reasoning_tokens"), cost_usd=cost, cost_status=status)

    # ── forecast ─────────────────────────────────────────────────────────────
    def forecast(self, b: _budget.Budget | None = None) -> dict | None:
        """The cached forecast for the budget's scope, refreshing it when due (models in the background)."""
        b = b or self.budget()
        key = f"forecast:{b.provider or '*'}"
        cached = self.store.meta(key)
        now = self.clock()
        hours = self.store.hourly(until=now, provider=b.provider)
        if not hours:
            return None
        newest = hours[-1][0]
        horizon = max(1, min(24 * 31, int((_budget.period_bounds(b.period, now)[1] - newest) // HOUR) + 1))
        tier, note = _forecast.choose(str(self._get("forecaster", "auto") or "auto"), bool(self._secret(SECRET)),
                                      self.data_dir, len(hours))
        if tier == "ephemeris" and (self.store.meta("ephemeris_failed_at") or 0) > now - FAIL_BACKOFF:
            tier, note = _forecast.choose("cascade", False, self.data_dir, len(hours))
        current = bool(cached) and cached.get("data_until") == newest and cached.get("horizon", 0) >= horizon
        age = now - cached["made_at"] if cached else float("inf")
        if tier == "seasonal":
            if current and cached["tier"] == "seasonal" and age < LOCAL_REFRESH:
                return cached
            cached = self._stamp(_forecast.seasonal(hours, horizon), newest, horizon, note)
            self.store.set_meta(key, cached)
            return cached
        if current and cached["tier"] == tier and age < 6 * HOUR:
            return cached
        if not current:   # stand in with the seasonal forecast until the model answers
            cached = self._stamp(_forecast.seasonal(hours, horizon), newest, horizon, f"{tier} forecast pending")
            self.store.set_meta(key, cached)
        if now - (self.store.meta(f"model_run:{key}") or 0) >= MODEL_REFRESH or not current:
            self._refresh_async(key, tier, hours, horizon, newest)
        return cached

    def _stamp(self, f, newest, horizon, note=""):
        f.update(data_until=newest, horizon=horizon, note=note)
        return f

    def _refresh_async(self, key, tier, hours, horizon, newest):
        with self._lock:
            if self._refreshing:
                return
            self._refreshing = True
        self.store.set_meta(f"model_run:{key}", self.clock())

        def run():
            try:
                if tier == "ephemeris":
                    f = _forecast.ephemeris(hours, horizon, self._secret(SECRET),
                                            str(self._get("ephemeris_model", "toto2-313m") or ""))
                else:
                    f = _forecast.cascade(hours, horizon, self.data_dir)
                self.store.set_meta(key, self._stamp(f, newest, horizon))
            except Exception as e:  # stay on the local forecast; say why in /obol
                if tier == "ephemeris":
                    self.store.set_meta("ephemeris_failed_at", self.clock())
                self.store.set_meta("last_error", {"tier": tier, "error": str(e)[:200], "at": self.clock()})
                log.warning("obol: %s forecast failed: %s", tier, e)
            finally:
                with self._lock:
                    self._refreshing = False

        threading.Thread(target=run, name="obol-forecast", daemon=True).start()

    def outlook(self, b: _budget.Budget | None = None) -> _budget.Outlook | None:
        b = b or self.budget()
        if not b.active:
            return None
        now = self.clock()
        start, _ = _budget.period_bounds(b.period, now)
        spent = self.store.spent(start, now, b.provider)
        this_hour = self.store.spent(int(now // HOUR) * HOUR, now, b.provider)
        return _budget.outlook(b, spent, this_hour, self.forecast(b), now)

    # ── acting ───────────────────────────────────────────────────────────────
    def _in_scope(self, b, provider: str) -> bool:
        return not b.provider or not provider or provider == b.provider

    def pre_llm_call(self, **kw):
        b = self.budget()
        if not b.active:
            return None
        sid = kw.get("session_id") or ""
        if not self._in_scope(b, self.store.last_provider(sid)):
            return None
        o = self.outlook(b)
        if o is None:
            return None
        st = o.state
        if st == "out" and b.mode == "guard":
            return {"action": "block", "message": (
                f"Obol: the {b.period}ly budget is used up ({_budget.line(o)}). New turns are paused until "
                f"{_budget._when(o.end, self.clock())}. Raise it with /obol budget <usd>, or set /obol mode watch.")}
        # tell the agent once per session each time the outlook gets worse
        rank = {"ok": 0, "unknown": 0, "tight": 1, "over": 2, "out": 3}[st]
        seen_key = f"told:{sid}"
        if rank and rank > (self.store.meta(seen_key) or 0):
            self.store.set_meta(seen_key, rank)
            note = f"[Obol budget] {_budget.line(o)}."
            if st in ("over", "out"):
                note += " Prefer short answers and fewer tool calls; tell the user before starting long work."
            if b.mode == "steer" and b.cheap_model and st in ("over", "tight", "out"):
                note += f" Requests are being routed to {b.cheap_model} to stretch the budget."
            return {"context": note}
        return None

    def pre_tool_call(self, **kw):
        """Guard mode: a long turn can't keep spending past the limit through its tool loop."""
        b = self.budget()
        if not (b.active and b.mode == "guard"):
            return None
        now = self.clock()
        start, _ = _budget.period_bounds(b.period, now)
        if self.store.spent(start, now, b.provider) >= b.usd:
            return {"action": "block", "message": "Obol: budget used up; stop calling tools, summarise what is "
                                                  "done so far and what is left, then end the turn."}
        return None

    def llm_request(self, **kw):
        """Steer mode: when the forecast says the budget won't last, send this request to the cheap model
        (same provider: only the model id changes)."""
        b = self.budget()
        req = kw.get("request")
        if not (b.active and b.mode == "steer" and b.cheap_model and isinstance(req, dict) and "model" in req):
            return None
        if req.get("model") == b.cheap_model or not self._in_scope(b, kw.get("provider") or ""):
            return None
        o = self.outlook(b)
        if o is None or o.state not in ("over", "tight", "out"):
            return None
        new = dict(req)
        new["model"] = b.cheap_model
        self.store.set_meta("last_steer", {"at": self.clock(), "from": req.get("model"), "to": b.cheap_model,
                                           "state": o.state})
        return {"request": new, "source": "obol", "reason": f"budget {o.state}: {_budget.line(o)}"}

    # ── user surface ─────────────────────────────────────────────────────────
    def status(self) -> dict:
        b = self.budget()
        now = self.clock()
        o = self.outlook(b)
        out = {"budget": vars(b), "line": _budget.line(o, now) if o else "no budget set (/obol budget 20 week)"}
        if o:
            out.update(state=o.state, spent=round(o.spent, 4), period_end=o.end, forecaster=o.tier, model=o.model,
                       projected_end_median=o.end_median, projected_end_high_pace=o.end_high,
                       runs_out_at=o.out_median, runs_out_at_high_pace=o.out_high,
                       unpriced_requests=self.store.unpriced(o.start, b.provider))
        f = self.store.meta(f"forecast:{b.provider or '*'}") or {}
        if f.get("note"):
            out["forecaster_note"] = f["note"]
        err = self.store.meta("last_error")
        if err and now - err["at"] < FAIL_BACKOFF:
            out["last_error"] = err
        if steer := self.store.meta("last_steer"):
            out["last_steer"] = steer
        return out


def _read_secret(name: str) -> str | None:
    try:
        from agent.secret_scope import get_secret
        return get_secret(name) or None
    except Exception:
        return None




# ── registration ─────────────────────────────────────────────────────────────

_obol: Obol | None = None


def _safe(fn):
    def wrapped(**kw):
        try:
            return fn(**kw)
        except Exception:
            log.warning("obol: %s failed", fn.__name__, exc_info=True)
            return None
    wrapped.__name__ = fn.__name__
    return wrapped


def register(ctx):
    global _obol
    _obol = Obol(ctx)
    from . import _commands
    for hook in ("post_api_request", "post_auxiliary_call"):
        ctx.register_hook(hook, _safe(_obol.meter))
    ctx.register_hook("pre_llm_call", _safe(_obol.pre_llm_call))
    ctx.register_hook("pre_tool_call", _safe(_obol.pre_tool_call))
    ctx.register_middleware("llm_request", _safe(_obol.llm_request))
    ctx.register_command("obol", lambda raw_args="", **_: _commands.run(_obol, raw_args),
                         description="Budget, forecast and run-out time for this agent's spend",
                         args_hint="[budget <usd> <day|week|month> | mode <watch|steer|guard> | cheap <model> | "
                                   "forecast | models | model install]")
    ctx.register_tool(name="obol_budget", toolset="obol", schema=_commands.TOOL_SCHEMA,
                      handler=lambda args=None, **_: json.dumps(_obol.status(), default=str))
