"""Obol plugin core.

Meters every provider request (main loop and auxiliary calls), forecasts hourly spend, and depending on
the budget's mode tells you and the agent (alert), switches to a cheaper model when the forecast says the
budget won't last (save), or stops new turns and tools once it is used up (stop).
"""
from __future__ import annotations

import dataclasses
import json
import logging
import threading
import time
from pathlib import Path

from . import _backfill, _budget, _forecast, _rules
from ._pricing import Prices
from ._store import HOUR, Store

log = logging.getLogger("hermes_plugins.obol")

SECRET = "EPHEMERIS_API_KEY"
LOCAL_REFRESH = 10 * 60          # the seasonal forecast is cheap: recompute this often
MODEL_REFRESH = HOUR             # local model / Ephemeris: at most hourly, and only after a new hour of data
FAIL_BACKOFF = 3 * HOUR          # after a forecaster fails, leave it alone this long
STEER_PAUSE = 6 * HOUR           # after the provider rejects the cheap model
ALLOW_FOR = HOUR                 # /obol allow: how long it waits for a turn, and how long that turn's tools run
# Providers that serve only their own models. Routers (OpenRouter and the like) can serve anything.
OWN_MODELS = {"anthropic": ("claude",), "openai": ("gpt", "chatgpt", "o1", "o3", "o4"), "deepseek": ("deepseek",),
              "gemini": ("gemini",), "google": ("gemini",), "xai": ("grok",)}


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

    def learn_history(self) -> dict:
        """Once per profile: import past usage from Hermes's session log so forecasts start right away."""
        if self.store.meta("history_imported") is not None:
            return self.store.meta("history_imported")
        if (self.store.meta("history_failed_at") or 0) > self.clock() - HOUR:
            return {}
        with self._lock:
            if self.store.meta("history_imported") is not None:
                return self.store.meta("history_imported")
            try:
                got = _backfill.import_history(self.store, self.data_dir, self.prices.cost, self.clock())
            except Exception as e:                # a locked state.db, say: try again later, not on every hook
                log.warning("obol: couldn't read past usage: %s", e)
                self.store.set_meta("history_failed_at", self.clock())
                return {}
            self.store.set_meta("history_imported", got or {"sessions": 0})
            return got

    @property
    def prices(self) -> Prices:
        if self._prices is None:
            self._prices = Prices(self.data_dir, overrides=lambda: self._get("prices", {}) or {})
        return self._prices

    # ── settings ─────────────────────────────────────────────────────────────
    def budget(self) -> _budget.Budget:
        return _budget.Budget.from_config(self._get)

    def budgets(self) -> list:
        """Every active budget: the main one (all providers, or the one named in `provider`) plus one per entry in
        `budgets` ([{provider, usd, period}]). Mode and cheaper model are shared."""
        main = self.budget()
        out = [main] if main.active else []
        for e in self._get("budgets", []) or []:
            if not isinstance(e, dict) or not e.get("provider"):
                continue
            usd = _budget.number(e.get("usd"))
            period = e.get("period") if e.get("period") in _budget.PERIODS else "week"
            if usd > 0:
                out.append(dataclasses.replace(main, usd=usd, period=period, provider=str(e["provider"])))
        return out

    def set_provider_budget(self, provider: str, usd: float, period: str) -> None:
        entries = [e for e in (self._get("budgets", []) or []) if isinstance(e, dict) and e.get("provider") != provider]
        if usd > 0:
            entries.append({"provider": provider, "usd": usd, "period": period})
        self._set("budgets", entries)

    def topup(self, amount: float, provider: str = "") -> _budget.Budget | None:
        """Add to the current period of the budget for `provider` ("" = the main budget)."""
        if not amount > 0 or amount == float("inf"):
            raise ValueError("a top-up must be a positive amount")
        main = self.budget()
        b = main if provider in ("", main.provider) else next((x for x in self.budgets() if x.provider == provider), None)
        if b is None or not b.active:
            return None
        start, _ = _budget.period_bounds(b.period, self.clock())
        key = f"topup:{b.provider or '*'}:{b.period}:{int(start)}"
        self.store.set_meta(key, float(self.store.meta(key) or 0) + amount)
        self._log("top-up", "you", f"+${amount:g} this {b.period}" + (f" on {provider}" if provider else ""))
        return b

    def label(self, b) -> str:
        return f"{b.provider or 'all providers'}, {_budget.ADJ[b.period]}"

    # ── meter ────────────────────────────────────────────────────────────────
    def meter(self, **kw):
        """post_api_request / post_auxiliary_call: record tokens and cost. Never raises, never blocks on network."""
        usage = kw.get("usage")
        if not isinstance(usage, dict):
            return
        self.learn_history()      # before the first row, so history ends where Obol's own data begins
        model = kw.get("response_model") or kw.get("model") or ""
        provider = kw.get("provider") or ""
        if provider in ("", "auto"):        # background calls can report "auto": charge them to the real provider
            provider = self.store.last_provider(kw.get("session_id") or "") or self.store.latest_provider() or provider
        try:
            cost, status = self.prices.cost(model, usage)
        except Exception:                   # the request still happened: record it, unpriced
            log.warning("obol: couldn't price %s", model, exc_info=True)
            cost, status = None, "unpriced"
        if self.prices.stale(self.clock()):
            self.prices.refresh_async(on_done=lambda: self.store.reprice(self.prices.cost))
        self.store.add_call(
            session_id=kw.get("session_id") or "", platform=kw.get("platform") or "",
            provider=provider, model=model, aux_task=kw.get("aux_task") or "",
            input_tokens=usage.get("input_tokens"), output_tokens=usage.get("output_tokens"),
            cache_read_tokens=usage.get("cache_read_tokens"), cache_write_tokens=usage.get("cache_write_tokens"),
            reasoning_tokens=usage.get("reasoning_tokens"), cost_usd=cost, cost_status=status)

    # ── forecast ─────────────────────────────────────────────────────────────
    def forecast(self, b: _budget.Budget | None = None) -> dict | None:
        """The cached forecast for the budget's scope, refreshing it when due (models in the background)."""
        b = b or self.budget()
        key = f"forecast:{b.provider or '*'}:{b.period}"
        cached = self.store.meta(key)
        now = self.clock()
        hours = self.store.hourly(until=now, provider=b.provider)
        if not hours:
            return None
        newest = hours[-1][0]
        horizon = max(1, min(24 * 31, int((_budget.period_bounds(b.period, now)[1] - newest) // HOUR) + 1))
        key_value = self._secret(SECRET)   # read here: Hermes's secret scope doesn't follow into threads
        tier, note = _forecast.choose(str(self._get("forecaster", "auto") or "auto"), bool(key_value),
                                      self.data_dir, len(hours))
        if tier == "ephemeris" and (self.store.meta("ephemeris_failed_at") or 0) > now - FAIL_BACKOFF:
            tier, note = _forecast.choose("cascade", False, self.data_dir, len(hours))
        if tier == "cascade" and (self.store.meta("cascade_failed_at") or 0) > now - FAIL_BACKOFF:
            tier, note = "seasonal", "cascade failed"
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
        if tier == "cascade":   # ~50 ms on a CPU: run it here, no thread, no stand-in
            try:
                tokens = self.store.hourly_tokens(until=now, provider=b.provider)
                week = now - 7 * 86400
                tok_week = sum(v for h, v in tokens if h >= week)
                # both sides over completed hours: the current hour's spend has no tokens in `tokens` yet
                done = int(now // HOUR) * HOUR
                rate = (self.store.spent(week, done, b.provider) / tok_week) if tok_week else None
                cached = self._stamp(_forecast.cascade(hours, horizon, tokens=tokens, usd_per_token=rate),
                                     newest, horizon, note)
            except Exception as e:
                self.store.set_meta("last_error", {"tier": tier, "error": str(e)[:200], "at": now})
                self.store.set_meta("cascade_failed_at", now)
                log.warning("obol: cascade forecast failed: %s", e)
                cached = self._stamp(_forecast.seasonal(hours, horizon), newest, horizon, "cascade failed")
            self.store.set_meta(key, cached)
            return cached
        if not current:   # stand in with the seasonal forecast until the model answers
            cached = self._stamp(_forecast.seasonal(hours, horizon), newest, horizon, f"{tier} forecast pending")
            self.store.set_meta(key, cached)
        if now - (self.store.meta(f"model_run:{key}") or 0) >= MODEL_REFRESH or not current:
            self._refresh_async(key, tier, hours, horizon, newest, key_value)
        return cached

    def _stamp(self, f, newest, horizon, note=""):
        f.update(data_until=newest, horizon=horizon, note=note)
        return f

    def _refresh_async(self, key, tier, hours, horizon, newest, api_key=None):
        with self._lock:
            if self._refreshing:
                return
            self._refreshing = True
        self.store.set_meta(f"model_run:{key}", self.clock())

        def run():
            try:
                if tier == "ephemeris":
                    f = _forecast.ephemeris(hours, horizon, api_key,
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

    def _topup(self, b, now) -> float:
        start, _ = _budget.period_bounds(b.period, now)
        return float(self.store.meta(f"topup:{b.provider or '*'}:{b.period}:{int(start)}") or 0)

    def outlook(self, b: _budget.Budget | None = None) -> _budget.Outlook | None:
        b = b or self.budget()
        if not b.active:
            return None
        now = self.clock()
        extra = self._topup(b, now)
        if extra:
            b = dataclasses.replace(b, usd=b.usd + extra)
        start, _ = _budget.period_bounds(b.period, now)
        spent = self.store.spent(start, now, b.provider)
        this_hour = self.store.spent(int(now // HOUR) * HOUR, now, b.provider)
        return _budget.outlook(b, spent, this_hour, self.forecast(b), now)

    # ── what the rules see ───────────────────────────────────────────────────
    def _usual_day(self, provider: str, now: float):
        """Mean daily spend over the last (up to) 14 full days; None with under 7 days of history."""
        first = self.store.meta("first_call_at")
        today, _ = _budget.period_bounds("day", now)
        if not first or today - float(first) < 7 * 86400:
            return None
        days = min(14, int((today - float(first)) // 86400))
        total = self.store.spent(today - days * 86400, today, provider)
        return total / days if total > 0 else None

    def _today_expected(self, provider: str, forecast, now: float) -> float:
        today, tomorrow = _budget.period_bounds("day", now)
        total = self.store.spent(today, now, provider)
        if forecast:
            path = forecast.get("mean") or forecast["quantiles"].get("0.5") or []
            current = int(now // HOUR) * HOUR
            this_hour = self.store.spent(current, now, provider)
            for i, v in enumerate(path):
                h = forecast["start"] + i * HOUR
                if v is None or h + HOUR <= now:
                    continue
                if h >= tomorrow:
                    break
                total += max(0.0, v - this_hour) if h == current else v
        return total

    @staticmethod
    def _tokens(messages) -> int:
        chars = 0
        for m in messages if isinstance(messages, list) else [messages]:
            c = m.get("content") if isinstance(m, dict) else m
            chars += len(c) if isinstance(c, str) else len(str(c or ""))
        return chars // 4                                   # about 4 characters a token

    def estimate_usd(self, model: str, tokens: int) -> float | None:
        """Rough input cost of a prompt of `tokens` tokens (no cache discount, so an upper estimate)."""
        rate = self.prices.rate(model)
        return tokens * rate[0] / 1e6 if rate and tokens else None

    def context(self, *, session_id="", provider=None, model="", platform="", aux=False, request_usd=None) -> dict:
        now = self.clock()
        b = self.budget()
        if provider is None:                # a session's first turn has no rows: go by the model it's about to use
            provider = self.store.last_provider(session_id) or self.store.provider_of_model(model)
        ctx = {"provider": provider, "model": model, "platform": platform, "aux": aux, "request_usd": request_usd,
               "state": "ok", "runs_out_in": None, "spent_pct": None, "day_vs_usual": None, "line": ""}
        many = len(self.budgets()) > 1
        worst = None
        for bb in self.budgets():
            if not self._in_scope(bb, provider):
                continue
            o = self.outlook(bb)
            if o is None:
                continue
            key = (_rules.SEVERITY.get(o.state, 0), -(o.out_median or float("inf")))
            if worst is None or key > worst[0]:
                worst = (key, bb, o)
        if worst:
            _, bb, o = worst
            line = _budget.line(o, now)
            ctx.update(state=o.state if o.state != "unknown" else "ok", spent_pct=100 * o.spent / o.usd if o.usd > 0 else None,
                       runs_out_in=(o.out_median - now) if o.out_median else None,
                       line=f"{self.label(bb)}: {line}" if many else line, budget_provider=bb.provider)
        job = self.store.meta("job_cap")
        if job:
            used = self.store.spent(job["since"], now, "")
            ctx["job"] = {"usd": job["usd"], "spent": used}
            if used >= job["usd"]:
                ctx["state"] = "out"
                ctx["line"] = f"task cap ${job['usd']:g} used up (${used:.2f} since it was set)"
        usual = self._usual_day(b.provider, now)
        if usual:
            today = self._today_expected(b.provider, self.forecast(b), now)
            ctx.update(day_vs_usual=today / usual, today_expected=today, usual_day=usual)
        return ctx

    def rules(self) -> list:
        raw = self._get("rules", []) or []
        if raw:
            return [r for r in (_rules.normalize(x) for x in raw) if r]
        b = self.budget()
        return [_rules.normalize(r) for r in _rules.presets(
            b.mode, b.cheap_model, _budget.number(self._get("unusual_day_x", 3)),
            _budget.number(self._get("max_request_usd", 0)))]

    def _paused(self) -> bool:
        return (self.store.meta("paused_until") or 0) > self.clock()

    def _log(self, action: str, rule: str, detail: str) -> None:
        log_ = self.store.meta("actions") or []
        log_.append({"at": self.clock(), "action": action, "rule": rule, "detail": detail[:240]})
        self.store.set_meta("actions", log_[-30:])

    def _in_scope(self, b, provider: str) -> bool:
        """A budget for one provider covers that provider only: an unknown provider answers to the others."""
        return not b.provider or provider == b.provider

    def _allowed_turn(self, sid: str) -> bool:
        at = self.store.meta(f"allowed_turn:{sid}")
        return bool(at) and self.clock() - at < ALLOW_FOR

    def _can_steer(self, target: str, current: str, provider: str) -> bool:
        """Only to a model this provider can serve, and only if it's actually cheaper (when both prices are known)."""
        own = OWN_MODELS.get((provider or "").lower())
        if own and not target.rsplit("/", 1)[-1].lower().startswith(own):
            return False
        new, old = self.prices.rate(target), self.prices.rate(current)
        return not (new and old and new[0] >= old[0] and new[1] >= old[1])

    # ── acting ───────────────────────────────────────────────────────────────
    def pre_llm_call(self, **kw):
        self.learn_history()
        if self._paused():
            return None
        sid = kw.get("session_id") or ""
        self.store.set_meta(f"allowed_turn:{sid}", None)    # runs once per turn: last turn's allowance is over
        # The hook sees the conversation but not the system prompt or tool definitions, so start from the size
        # of the last real request (which includes them) plus the new message.
        new_msg = self._tokens([kw.get("user_message") or ""])
        tokens = max(self.store.last_prompt_tokens(sid) + new_msg, self._tokens(kw.get("conversation_history") or []))
        ctx = self.context(session_id=sid, model=kw.get("model") or "", platform=kw.get("platform") or "",
                           request_usd=self.estimate_usd(kw.get("model") or "", tokens))
        ctx["request_tokens"] = tokens
        acts = _rules.evaluate(self.rules(), ctx)
        stops = [a for a in acts if a[1] in ("block", "ask")]
        allow = self.store.meta("allow_once")
        if stops and allow and self.clock() - allow < ALLOW_FOR:
            self.store.set_meta("allow_once", None)
            self.store.set_meta(f"allowed_turn:{sid}", self.clock())   # its tools too, or the turn can't do anything
            self._log("allowed", stops[0][0]["name"], "one turn let through by /obol allow")
            stops = []
        blocks = [a for a in stops if a[1] == "block"]
        if blocks:
            r, _, v = blocks[0]
            msg = v if isinstance(v, str) else (f"Obol: {ctx['line'] or 'budget used up'}. New turns are paused. "
                                                "Add to it with /obol topup 5, or /obol allow to send one turn.")
            self._log("block", r["name"], ctx["line"])
            return {"action": "block", "message": msg if msg.startswith("Obol") else f"Obol: {msg}"}
        if stops:
            r, _, v = stops[0]
            why = (f"this turn would send about {ctx['request_tokens']:,} tokens (~{_budget._money(ctx['request_usd'])})"
                   if ctx.get("request_usd") is not None and "request_usd" in r["when"] else ctx["line"])
            self._log("ask", r["name"], why)
            return {"action": "block", "message": f"Obol: {v if isinstance(v, str) else 'check first'}: {why}. "
                                                  "Send /obol allow to let this one through."}
        notes, steer_to = [], None
        for r, action, v in acts:
            key = f"{r['name']}:{ctx['state']}:{ctx.get('budget_provider', '')}"
            if action == "steer" and steer_to is None:
                steer_to = _rules.steer_target(v, ctx["state"])
            elif action == "note" and self.store.meta(f"told:{sid}:{key}") is None:
                self.store.set_meta(f"told:{sid}:{key}", self.clock())
                text = v if isinstance(v, str) else f"[Obol budget] {ctx['line'] or 'spend is above normal'}."
                if not isinstance(v, str) and ctx["state"] in ("over", "out"):
                    text += " Prefer short answers and fewer tool calls; tell the user before starting long work."
                notes.append(text)
            elif action == "notify":
                day = int(_budget.period_bounds("day", self.clock())[0])
                if self.store.meta(f"notified:{key}:{day}") is None:
                    self.store.set_meta(f"notified:{key}:{day}", self.clock())
                    self._queue_notice(sid, v if isinstance(v, str) else self._notice_text(r, ctx))
        if notes and steer_to:
            notes.append(f"Requests are being routed to {steer_to} to stretch the budget.")
        return {"context": " ".join(notes)} if notes else None

    def _notice_text(self, rule, ctx) -> str:
        if "day_vs_usual" in rule["when"] and ctx.get("day_vs_usual"):
            return (f"on track for ${ctx['today_expected']:.2f} today, about {ctx['day_vs_usual']:.1f}× your usual "
                    f"day (${ctx['usual_day']:.2f}).")
        return ctx["line"] or "budget alert"

    def _queue_notice(self, sid, text) -> None:
        pending = self.store.meta(f"notice:{sid}") or []
        pending.append(text)
        self.store.set_meta(f"notice:{sid}", pending[-3:])
        self._log("notify", "alert", text)

    def transform_llm_output(self, **kw):
        """Deliver queued notices to the person: one line under this turn's reply."""
        sid = kw.get("session_id") or ""
        pending = self.store.meta(f"notice:{sid}")
        text = kw.get("response_text")
        if not pending or not isinstance(text, str):
            return None
        self.store.set_meta(f"notice:{sid}", None)
        return text + "\n\n" + "\n".join(f"⚠ Obol: {p}" for p in pending)

    def pre_tool_call(self, **kw):
        """A long turn can't keep spending past the limit through its tool loop."""
        if self._paused():
            return None
        sid = kw.get("session_id") or ""
        if self._allowed_turn(sid):
            return None
        ctx = self.context(session_id=sid, platform=kw.get("platform") or "")
        acts = [a for a in _rules.evaluate(self.rules(), ctx) if a[1] in ("block", "ask")]
        if not acts:
            return None
        r, action, v = next((a for a in acts if a[1] == "block"), acts[0])
        if action == "block":
            self._log("block tool", r["name"], ctx["line"])
            return {"action": "block", "message": "Obol: budget used up; stop calling tools, summarise what is "
                                                  "done so far and what is left, then end the turn."}
        self._log("ask tool", r["name"], ctx["line"])
        return {"action": "approve", "rule_key": "obol",
                "message": f"Obol: {v if isinstance(v, str) else ctx['line'] or 'budget check'}. Run this tool anyway?"}

    def llm_request(self, **kw):
        """Steer rules: send this request to a cheaper model on the same provider (only the model id changes)."""
        req = kw.get("request")
        if self._paused() or not isinstance(req, dict) or "model" not in req:
            return None
        tokens = self._tokens(req.get("messages") or req.get("input") or []) + len(json.dumps(req.get("tools") or [])) // 4
        provider = kw.get("provider") or None
        ctx = self.context(session_id=kw.get("session_id") or "", provider=provider, model=req.get("model") or "",
                           platform=kw.get("platform") or "", request_usd=self.estimate_usd(req.get("model"), tokens))
        target = None
        for r, action, v in _rules.evaluate(self.rules(), ctx):
            if action == "steer":
                target, rule = _rules.steer_target(v, ctx["state"]), r
                break
        if not target or target == req.get("model"):
            return None
        if not self._can_steer(target, req.get("model"), ctx["provider"]):
            return None
        bad = self.store.meta("steer_failed")
        if (bad and bad.get("model") == target and bad.get("provider", "") in ("", ctx["provider"])
                and self.clock() - bad["at"] < STEER_PAUSE):
            return None
        new = dict(req)
        new["model"] = target
        self.store.set_meta("last_steer", {"at": self.clock(), "from": req.get("model"), "to": target,
                                           "state": ctx["state"], "provider": ctx["provider"]})
        if (self.store.meta("last_steer_logged") or "") != f"{req.get('model')}>{target}":
            self.store.set_meta("last_steer_logged", f"{req.get('model')}>{target}")
            self._log("steer", rule["name"], f"{req.get('model')} → {target} ({ctx['line']})")
        return {"request": new, "source": "obol", "reason": f"rule '{rule['name']}': {ctx['line']}"}

    def api_request_error(self, **kw):
        """If the provider rejects the cheap model right after a steer (not allowed, not found), stop steering
        to it for a while so every turn doesn't fail; the next attempt goes out on the normal model."""
        steer = self.store.meta("last_steer")
        if not steer or self.clock() - steer["at"] > 120:
            return None
        if kw.get("model") and kw["model"] != steer["to"]:   # some other request's failure
            return None
        if kw.get("status_code") in (400, 403, 404, 422):     # 401 is the key, not the model
            msg = str((kw.get("error") or {}).get("message") or kw.get("reason") or "")[:200]
            self.store.set_meta("steer_failed", {"model": steer["to"], "at": self.clock(), "error": msg,
                                                "provider": steer.get("provider", "")})
            log.warning("obol: provider rejected cheap model %s; steering paused", steer["to"])
        return None

    def providers(self, days: int = 30) -> list:
        """One row per provider seen lately: spend and expected total this period, its own $/M-token price, and its
        budget's outlook when it has one. Each provider gets its own token forecast at its own price."""
        now = self.clock()
        main = self.budget()
        period = main.period
        start, end = _budget.period_bounds(period, now)
        out = []
        for prov in self.store.providers(now - days * 86400):
            b = dataclasses.replace(main, provider=prov, usd=1e12, period=period)
            spent = self.store.spent(start, now, prov)
            this_hour = self.store.spent(int(now // HOUR) * HOUR, now, prov)
            o = _budget.outlook(b, spent, this_hour, self.forecast(b), now)
            week = now - 7 * 86400
            tok = sum(v for h, v in self.store.hourly_tokens(until=now, provider=prov) if h >= week)
            usd7 = self.store.spent(week, int(now // HOUR) * HOUR, prov)
            own = next((x for x in self.budgets() if x.provider == prov), None)
            ob = self.outlook(own) if own else None
            out.append({"provider": prov, "spent": spent, "expected": o.end_median, "period": period,
                        "usd_per_mtok": (usd7 / tok * 1e6) if tok else None,
                        "budget": own and {"usd": own.usd, "period": own.period, "line": _budget.line(ob, now)}})
        return sorted(out, key=lambda r: -(r["expected"] or r["spent"]))

    def unbudgeted(self) -> dict:
        """Before a budget is set: spend this period, a usual period, and where this one is heading."""
        now = self.clock()
        b = self.budget()
        start, end = _budget.period_bounds(b.period, now)
        spent = self.store.spent(start, now, b.provider)
        first = self.store.meta("first_call_at")
        out = {"period": b.period, "spent": spent, "usual": None, "expected": None}
        if first:
            days = min(28.0, (start - float(first)) / 86400)
            if days >= 3:
                per_day = self.store.spent(start - days * 86400, start, b.provider) / days
                out["usual"] = per_day * (end - start) / 86400
        o = _budget.outlook(dataclasses.replace(b, usd=1e12), spent,
                            self.store.spent(int(now // HOUR) * HOUR, now, b.provider), self.forecast(b), now)
        out["expected"] = o.end_median
        return out

    # ── user surface ─────────────────────────────────────────────────────────
    def status(self) -> dict:
        learned = self.learn_history()
        now = self.clock()
        rows = []
        for bb in self.budgets():
            ob = self.outlook(bb)
            if ob:
                rows.append((bb, ob))
        rows.sort(key=lambda r: (_rules.SEVERITY.get(r[1].state, 0), -(r[1].out_median or float("inf"))), reverse=True)
        b, o = rows[0] if rows else (self.budget(), None)
        out = {"budget": vars(b), "line": _budget.line(o, now) if o else "no budget set (/obol budget 20 week)"}
        if o:
            out.update(state=o.state, spent=round(o.spent, 4), period_end=o.end, forecaster=o.tier, model=o.model,
                       expected_period_total=o.end_median, runs_out_at=o.out_median,
                       unpriced_requests=self.store.unpriced(o.start, b.provider))
        out["budgets"] = [{"provider": bb.provider, "period": bb.period, "usd": bb.usd, "state": ob.state,
                           "label": self.label(bb), "line": _budget.line(ob, now)} for bb, ob in rows]
        out["ephemeris_key"] = bool(self._secret(SECRET))
        if learned and learned.get("sessions"):
            out["learned"] = learned
        f = self.store.meta(f"forecast:{b.provider or '*'}:{b.period}") or {}
        if f.get("note"):
            out["forecaster_note"] = f["note"]
        err = self.store.meta("last_error")
        if err and now - err["at"] < FAIL_BACKOFF:
            out["last_error"] = err
        bad = self.store.meta("steer_failed")
        if bad and now - bad["at"] < STEER_PAUSE:
            out["steer_paused"] = bad
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
    ctx.register_hook("api_request_error", _safe(_obol.api_request_error))
    ctx.register_hook("transform_llm_output", _safe(_obol.transform_llm_output))
    ctx.register_middleware("llm_request", _safe(_obol.llm_request))
    ctx.register_command("obol", lambda raw_args="", **_: _commands.run(_obol, raw_args),
                         description="Budget, forecast and run-out time for this agent's spend",
                         args_hint="[budget <usd> <day|week|month> | mode <alert|save|stop> | topup <usd> | pause 2h | "
                                   "more]")
    ctx.register_tool(name="obol_budget", toolset="obol", schema=_commands.TOOL_SCHEMA,
                      handler=lambda args=None, **_: json.dumps(_obol.status(), default=str))
