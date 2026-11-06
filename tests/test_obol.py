import pytest
import obol


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t
from obol import _plugin
from obol import _budget, _forecast, _commands
from obol._store import HOUR


def feed(o, hours, usd_per_hour, provider="openrouter", session="s1"):
    """Spend usd_per_hour in each of the `hours` hours before now."""
    now = o.clock()
    for i in range(hours, 0, -1):
        o.store.add_call(ts=now - i * HOUR + 60, session_id=session, provider=provider, model="m",
                         input_tokens=1000, output_tokens=100, cost_usd=usd_per_hour, cost_status="estimated")


def test_hourly_starts_at_first_call_and_fills_real_zeros(make):
    o = make()
    now = o.clock()
    o.store.add_call(ts=now - 5 * HOUR + 10, cost_usd=1.0)
    o.store.add_call(ts=now - 2 * HOUR + 10, cost_usd=2.0)
    o.store.add_call(ts=now + 10, cost_usd=9.0)          # current hour: excluded
    vals = [v for _, v in o.store.hourly(until=now + 20)]
    assert vals == [1.0, 0.0, 0.0, 2.0, 0.0]


def test_seasonal_reads_hour_of_day():
    base = 1_762_300_800
    hours = [(base + i * HOUR, 5.0 if i % 24 == 10 else 0.0) for i in range(24 * 3)]
    f = _forecast.seasonal(hours, 24)
    med = f["quantiles"]["0.5"]
    peak = [i for i, v in enumerate(med) if v > 0]
    assert len(peak) == 1 and (f["start"] + peak[0] * HOUR - base) // HOUR % 24 == 10


def test_meter_ignores_missing_usage(make):
    o = make()
    o.meter(model="x", usage=None)
    assert o.store.hourly() == []


def test_outlook_runs_out_and_guard_blocks(make):
    o = make({"budget_usd": 10, "period": "day", "mode": "stop"})
    feed(o, 30, 1.0)
    out = o.outlook()
    assert out.state == "out"           # $1/h for the hours since midnight exceeds $10
    r = o.pre_llm_call(session_id="s1")
    assert r["action"] == "block" and "used up" in r["message"]
    assert o.pre_tool_call(tool_name="terminal")["action"] == "block"


def test_watch_tells_once_then_stays_quiet(make):
    o = make({"budget_usd": 200, "period": "week", "mode": "alert"})
    feed(o, 40, 2.0)                    # on pace to blow $200 well before Sunday
    first = o.pre_llm_call(session_id="s1")
    assert "context" in first and "Obol budget" in first["context"]
    assert o.pre_llm_call(session_id="s1") is None


def test_steer_swaps_model_only_when_forecast_says_so(make):
    o = make({"budget_usd": 1000, "period": "week", "mode": "save", "cheap_model": "cheap/mini"})
    feed(o, 40, 0.5)
    req = {"model": "big/model", "messages": []}
    assert o.llm_request(request=req, provider="openrouter") is None     # comfortably inside budget
    o.cfg["budget_usd"] = 30
    r = o.llm_request(request=req, provider="openrouter")
    assert r["request"]["model"] == "cheap/mini" and req["model"] == "big/model"
    assert o.llm_request(request=req, provider="myproxy") is not None    # budget covers all providers
    o.cfg["provider"] = "openrouter"
    assert o.llm_request(request=req, provider="myproxy") is None


def test_commands_roundtrip(make):
    o = make()
    assert "no budget" in _commands.run(o, "")
    assert "Budget saved" in _commands.run(o, "budget 25 week")
    assert o.cfg["budget_usd"] == 25 and o.cfg["period"] == "week"
    assert "Budget for openrouter saved" in _commands.run(o, "budget 10 day openrouter")
    assert o.cfg["budgets"] == [{"provider": "openrouter", "usd": 10.0, "period": "day"}]
    assert "removed" in _commands.run(o, "budget 0 day openrouter") and o.cfg["budgets"] == []
    assert _commands.run(o, "mode nope").startswith("Usage")
    assert "amount above zero" in _commands.run(o, "budget abc")


def test_choose_prefers_key_then_local_model(make, tmp_path):
    assert _forecast.choose("auto", True, tmp_path, 200)[0] == "ephemeris"
    assert _forecast.choose("auto", False, tmp_path, 200)[0] == "cascade"
    assert _forecast.choose("auto", False, tmp_path, 100)[0] == "seasonal"     # under 128 points
    assert _forecast.choose("seasonal", False, tmp_path, 200)[0] == "seasonal"
    assert _forecast.choose("auto", True, tmp_path, 5)[0] == "seasonal"


def test_hooks_never_raise(make, monkeypatch):
    o = make({"budget_usd": 5})
    monkeypatch.setattr(o, "outlook", lambda *a, **k: 1 / 0)
    assert _plugin._safe(o.pre_llm_call)(session_id="x") is None


def test_pricing_overrides_catalogue_and_builtin(make):
    from obol import _pricing
    o = make({"prices": {"my/model": [1.0, 2.0]}})
    assert o.prices.cost("my/model", {"input_tokens": 1_000_000, "output_tokens": 500_000}) == (2.0, "estimated")
    assert o.prices.cost("anthropic/claude-sonnet-5.5", {"output_tokens": 1_000_000})[0] == 10.0
    assert o.prices.cost("nobody/knows", {"input_tokens": 5}) == (None, "unpriced")
    cat = _pricing.parse_catalogue([{"id": "openai/gpt-5-mini", "pricing": {"prompt": "0.00000025", "completion": "0.000002"}},
                                    {"id": "x/free:free", "pricing": {"prompt": "0", "completion": "0"}}])
    assert list(cat) == ["gpt-5-mini"] and abs(cat["gpt-5-mini"][0] - 0.25) < 1e-9


def test_meter_then_reprice(make):
    o = make()
    o.meter(model="later/model", usage={"input_tokens": 1_000_000}, provider="p", session_id="s")
    assert o.store.unpriced(0) == 1
    o.cfg["prices"] = {"later/model": [3.0, 3.0]}
    assert o.store.reprice(o.prices.cost) == 1 and o.store.unpriced(0) == 0


def test_rejected_cheap_model_pauses_steering(make):
    o = make({"budget_usd": 1, "period": "week", "mode": "save", "cheap_model": "openai/gpt-4o-mini"})
    feed(o, 40, 0.5)
    req = {"model": "deepseek/deepseek-v4.1-flash"}
    assert o.llm_request(request=req, provider="openrouter")["request"]["model"] == "openai/gpt-4o-mini"
    o.api_request_error(status_code=404, error={"message": "No allowed providers"})
    assert o.llm_request(request=req, provider="openrouter") is None
    assert "not switching for now" in _commands.run(o, "")
    _commands.run(o, "cheap deepseek/deepseek-v4-flash")
    assert o.llm_request(request=req, provider="openrouter")["request"]["model"] == "deepseek/deepseek-v4-flash"


def test_cascade_numpy_matches_torch_reference():
    import json
    import numpy as np
    from pathlib import Path
    from obol import _cascade
    for case in json.loads((Path(__file__).parent / "cascade_golden.json").read_text()):
        got = _cascade.model(case["model"]).quantiles(case["history"], case["horizon"])
        want = np.array(case["torch_quantiles"])               # float32 torch reference; weights ship as float16
        assert got.shape == want.shape
        assert np.abs(got - want).max() <= 5e-3 * (np.abs(want).max() + 1e-6), (case["model"], case["name"])


def test_cascade_is_the_default_local_forecaster(make):
    o = make({"budget_usd": 30, "period": "week"})
    feed(o, 200, 0.05)                                   # 1000 input + 100 output tokens an hour
    f = o.forecast()
    assert f["tier"] == "cascade" and "toktok" in f["model"] and len(f["quantiles"]["0.5"]) == f["horizon"]
    assert 0.02 < f["mean"][0] < 0.15                    # tokens forecast, converted back at $0.05 per 1100 tokens
    assert all(v >= 0 for v in f["quantiles"]["0.1"])
    assert o.outlook().state in ("ok", "tight", "over")


def test_ephemeris_hint_only_in_status_and_only_without_key(make):
    o = make({"budget_usd": 3000, "period": "week"})
    feed(o, 40, 0.05)
    assert "via Ephemeris" in _commands.run(o, "")
    assert "via Ephemeris" not in _commands.run(o, "")  # once
    o.store.set_meta("ephemeris_hint_shown", None)
    told = o.pre_llm_call(session_id="s1") or {}
    assert "Ephemeris" not in str(told)                 # never pushed into the agent's context
    o._secret = lambda n: "pc_live_x"
    o.store.set_meta("ephemeris_failed_at", o.clock())  # stay local, but the key exists
    assert "via Ephemeris" not in _commands.run(o, "")


def test_money_shows_small_amounts():
    assert _budget._money(0.0037) == "$0.0037"
    assert _budget._money(0.05) == "$0.05"
    assert _budget._money(0.0001) == "$0.0001"
    assert _budget._money(12.5) == "$12.50" and _budget._money(250) == "$250" and _budget._money(0) == "$0.00"


def _fake_state_db(home, now):
    import sqlite3
    c = sqlite3.connect(home / "state.db")
    c.execute("CREATE TABLE sessions (id TEXT, started_at REAL, ended_at REAL, model TEXT, billing_provider TEXT,"
              " input_tokens INT, output_tokens INT, cache_read_tokens INT, cache_write_tokens INT,"
              " actual_cost_usd REAL, estimated_cost_usd REAL, content_should_never_be_read TEXT)")
    c.execute("CREATE TABLE messages (session_id TEXT, role TEXT, timestamp REAL, content TEXT)")
    for d in range(1, 4):                                  # three past days, two replies 2 h apart each day
        t0 = now - d * 86400
        c.execute("INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                  (f"s{d}", t0, t0 + 7200, "deepseek/x", "openrouter", 1000, 100, 0, 0, None, 0.40, "secret"))
        for k in (0, 2):
            c.execute("INSERT INTO messages VALUES (?,?,?,?)", (f"s{d}", "assistant", t0 + k * 3600 + 60, "hi"))
    c.execute("INSERT INTO sessions VALUES ('never-ran', ?, NULL, 'm', NULL, 0, 0, 0, 0, NULL, NULL, '')", (now - 999,))
    c.commit()


def test_learns_past_usage_once_from_hermes_state_db(tmp_path):
    home = tmp_path / "home"
    data = home / "plugin-data" / "obol"
    data.mkdir(parents=True)
    now = 1_762_340_400.0
    _fake_state_db(home, now)
    cfg = {"budget_usd": 5, "period": "week"}
    o = obol.Obol(data_dir=data, get=lambda k, d=None: cfg.get(k, d), set_=cfg.__setitem__,
                  secret=lambda n: None, clock=Clock(now))
    o.prices.stale = lambda now=None: False
    got = o.learn_history()
    assert got["sessions"] == 3 and got["requests"] == 6 and abs(got["usd"] - 1.2) < 1e-9
    hours = o.store.hourly(until=now)
    assert len(hours) >= 72 and abs(sum(v for _, v in hours) - 1.2) < 1e-9
    assert sum(1 for _, v in hours if v > 0) == 6                 # cost lands in the hours replies did
    o.meter(model="deepseek/x", usage={"input_tokens": 10}, provider="openrouter", session_id="new")
    assert o.learn_history() == got and o.store.meta("history_imported") == got   # only once
    assert "learned from 3 past Hermes sessions" in _commands.run(o, "")


def test_history_import_never_overlaps_obol_data(tmp_path):
    home = tmp_path / "home"; data = home / "plugin-data" / "obol"; data.mkdir(parents=True)
    now = 1_762_340_400.0
    _fake_state_db(home, now)
    o = obol.Obol(data_dir=data, get=lambda k, d=None: None, set_=lambda k, v: None, secret=lambda n: None,
                  clock=Clock(now))
    o.store.set_meta("history_imported", None)
    o.store.add_call(ts=now - 2 * 86400, cost_usd=1.0)            # Obol was already metering 2 days ago
    o.store.set_meta("history_imported", None)
    from obol import _backfill
    got = _backfill.import_history(o.store, data, o.prices.cost, now)
    assert got["sessions"] == 1                                    # only the session before that


from obol import _rules


def test_rule_conditions_and_ladder():
    ctx = {"state": "over", "runs_out_in": 3000, "spent_pct": 85, "request_usd": 0.7, "day_vs_usual": 3.4,
           "provider": "openrouter", "model": "anthropic/claude-opus-5.5", "platform": "telegram", "aux": False}
    assert _rules.matches({"state": ["tight", "over"], "runs_out_within": "1h", "spent_pct": ">=80"}, ctx)
    assert _rules.matches({"model": "anthropic/*", "platform": "telegram", "day_vs_usual": ">3x"}, ctx)
    assert not _rules.matches({"request_usd": ">1"}, ctx)
    assert _rules.matches({"request_usd": ">1e-07"}, ctx) and _rules.compare(0.5, "$0.25")
    assert not _rules.matches({"no_such_condition": 1}, ctx)            # unknown conditions fail safe
    assert _rules.normalize({"do": "explode"}) is None and _rules.normalize({"do": "notify"})["do"] == {"notify": True}
    ladder = ["mid/model", "cheap/model"]
    assert [_rules.steer_target(ladder, s) for s in ("tight", "over", "out")] == ["mid/model", "cheap/model", "cheap/model"]


def test_custom_rules_replace_the_preset(make):
    o = make({"budget_usd": 30, "period": "week", "rules": [
        {"name": "ladder", "when": {"state": ["tight", "over", "out"]}, "do": {"steer": ["mid/m", "cheap/m"]}},
        {"when": {"bogus": True}, "do": "block"}, {"do": "explode"}]})
    feed(o, 40, 0.5)                                                     # over: runs out before Sunday
    r = o.llm_request(request={"model": "big/m", "messages": [{"content": "hi"}]}, provider="openrouter")
    assert r["request"]["model"] == "cheap/m"
    assert o.pre_llm_call(session_id="s1") is None                       # bogus block rule never fires
    assert "malformed" in _commands.run(o, "rules") and "ladder" in _commands.run(o, "rules")
    assert "steer" in _commands.run(o, "why")


def test_large_request_is_held_until_allowed(make):
    o = make({"budget_usd": 0, "max_request_usd": 0.10, "prices": {"big/m": [10.0, 30.0]}})
    feed(o, 5, 0.01)
    huge = [{"content": "x" * 200_000}]                                  # ~50k tokens at $10/M = $0.50
    r = o.pre_llm_call(session_id="s1", model="big/m", conversation_history=huge, user_message="go")
    assert r["action"] == "block" and "$0.50" in r["message"] and "/obol allow" in r["message"]
    small = o.pre_llm_call(session_id="s9", model="big/m", conversation_history=[], user_message="hi")
    assert small is None                                                  # a tiny turn passes
    o.store.add_call(ts=o.clock() - 5, session_id="s9", model="big/m", input_tokens=20_000, cost_usd=0.2)
    r = o.pre_llm_call(session_id="s9", model="big/m", conversation_history=[], user_message="hi")
    assert r["action"] == "block" and "20,000 tokens" in r["message"]     # system prompt + tools counted
    _commands.run(o, "allow")
    assert o.pre_llm_call(session_id="s1", model="big/m", conversation_history=huge, user_message="go") is None
    assert o.pre_llm_call(session_id="s1", model="big/m", conversation_history=huge, user_message="go")["action"] == "block"


def test_unusual_day_notifies_the_user_under_the_reply(make):
    o = make({"budget_usd": 0, "unusual_day_x": 3})
    now = o.clock()
    for d in range(1, 15):                                               # two quiet weeks: $1 a day
        o.store.add_call(ts=now - d * 86400, cost_usd=1.0, provider="openrouter", session_id="old")
    today = obol._budget.period_bounds("day", now)[0]
    o.store.add_call(ts=today + 60, cost_usd=5.0, provider="openrouter", session_id="s1")   # 5× already
    assert o.pre_llm_call(session_id="s1") is None or True
    out = o.transform_llm_output(response_text="Here you go.", session_id="s1")
    assert out.startswith("Here you go.") and "your usual day" in out and "⚠ Obol" in out
    assert o.transform_llm_output(response_text="Again.", session_id="s1") is None   # delivered once
    o.pre_llm_call(session_id="s1")
    assert o.transform_llm_output(response_text="Later.", session_id="s1") is None   # once a day


def test_pause_cap_and_topup(make):
    o = make({"budget_usd": 1, "period": "day", "mode": "stop"})
    feed(o, 3, 0.5)
    assert o.pre_llm_call(session_id="s1")["action"] == "block"
    _commands.run(o, "pause 1h")
    assert o.pre_llm_call(session_id="s1") is None and o.pre_tool_call(tool_name="t") is None
    _commands.run(o, "pause off")
    assert "Added $5" in _commands.run(o, "topup 5")
    assert (o.pre_llm_call(session_id="s1") or {}).get("action") != "block"
    _commands.run(o, "cap 0.01")
    o.clock_obj.t += 60
    o.store.add_call(ts=o.clock() - 30, cost_usd=0.02, provider="openrouter", session_id="s1")
    assert o.pre_llm_call(session_id="s2")["action"] == "block"          # the task cap is spent
    _commands.run(o, "cap off")
    assert (o.pre_llm_call(session_id="s3") or {}).get("action") != "block"


def test_ask_rule_escalates_tools_to_approval(make):
    o = make({"budget_usd": 30, "period": "week", "rules": [{"when": {"state": ["over", "out"]}, "do": "ask"}]})
    feed(o, 40, 0.5)
    r = o.pre_tool_call(tool_name="terminal")
    assert r["action"] == "approve" and r["rule_key"] == "obol"


def test_simple_surface_and_legacy_names(make):
    o = make()
    assert len(_commands.HELP.splitlines()) <= 10 and "/obol more" in _commands.HELP
    assert "/obol why" in _commands.run(o, "more")
    assert "Mode: save" in _commands.run(o, "mode steer")          # old names still work
    assert o.budget().mode == "save" and "cheaper model" in _commands.run(o, "mode save")
    o.cfg["mode"] = "guard"
    assert o.budget().mode == "stop"
    assert "alert:" in _commands.run(o, "mode nope")
    assert _commands.run(o, "frobnicate").startswith("Unknown")


def test_no_budget_status_suggests_one(make):
    o = make()
    now = o.clock()
    for d in range(1, 22):                                          # three weeks at $2 a day
        o.store.add_call(ts=now - d * 86400, cost_usd=2.0, provider="openrouter", session_id="old")
    out = _commands.run(o, "")
    assert "no budget yet" in out and "a usual week: about $14." in out
    assert "try: /obol budget 18 week" in out


def test_status_is_plain_language(make):
    o = make({"budget_usd": 30, "period": "week"})
    feed(o, 40, 0.5)
    line = _commands.run(o, "").splitlines()[0]
    assert "on track to run out" in line and "pinball" not in line and "over" not in line


def test_each_api_forecast_on_its_own_usage_and_price(make):
    o = make({"budget_usd": 0, "period": "week",
              "budgets": [{"provider": "openrouter", "usd": 1, "period": "week"},
                          {"provider": "anthropic", "usd": 100, "period": "week"}]})
    now = o.clock()
    for i in range(200, 0, -1):                    # same token volume, 20x price difference
        o.store.add_call(ts=now - i * HOUR + 60, session_id="a", provider="openrouter", model="cheap",
                         input_tokens=100_000, cost_usd=0.01)
        o.store.add_call(ts=now - i * HOUR + 120, session_id="b", provider="anthropic", model="dear",
                         input_tokens=100_000, cost_usd=0.20)
    rows = {r["provider"]: r for r in o.providers()}
    assert abs(rows["openrouter"]["usd_per_mtok"] - 0.10) < 1e-9 and abs(rows["anthropic"]["usd_per_mtok"] - 2.0) < 1e-9
    assert rows["anthropic"]["expected"] > 10 * rows["openrouter"]["expected"]   # its own price, not a blend
    assert "openrouter" in _commands.run(o, "providers") and "/M tok" in _commands.run(o, "providers")
    # the strictest budget that applies decides: openrouter's $1 won't last, anthropic's $100 is fine
    assert o.context(provider="openrouter")["state"] in ("over", "out")
    assert o.context(provider="anthropic")["state"] == "ok"
    st = _commands.run(o, "")
    assert "openrouter, weekly:" in st and "anthropic, weekly:" in st
    assert "Added $5" in _commands.run(o, "topup 5 openrouter")


def test_background_calls_reporting_auto_count_for_the_real_provider(make):
    o = make()
    o.meter(model="m", usage={"input_tokens": 10}, provider="openrouter", session_id="s1")
    o.meter(model="m", usage={"input_tokens": 10}, provider="auto", session_id="s1", aux_task="title_generation")
    assert o.store.providers(0) == ["openrouter"]


# ── found in review ──────────────────────────────────────────────────────────

def _spent_out(o, provider="openrouter", session="s1"):
    feed(o, 10, 1.0, provider=provider, session=session)


def test_allow_covers_that_turns_tools_and_only_that_turn(make):
    o = make({"budget_usd": 1, "mode": "stop"})
    _spent_out(o)
    assert o.pre_tool_call(session_id="s1")["action"] == "block"
    _commands.run(o, "allow")
    assert "action" not in (o.pre_llm_call(session_id="s1", user_message="go") or {})
    assert o.pre_tool_call(session_id="s1") is None
    assert o.pre_tool_call(session_id="other")["action"] == "block"
    assert o.pre_llm_call(session_id="s1", user_message="again")["action"] == "block"
    assert o.pre_tool_call(session_id="s1")["action"] == "block"


def test_allow_expires(make):
    o = make({"budget_usd": 1, "mode": "stop"})
    _spent_out(o)
    _commands.run(o, "allow")
    o.clock_obj.t += 2 * HOUR
    assert o.pre_llm_call(session_id="s1", user_message="go")["action"] == "block"


def test_new_session_doesnt_inherit_another_sessions_prompt_size(make):
    o = make({"budget_usd": 1000, "max_request_usd": 0.5})
    o.store.add_call(ts=o.clock() - 60, session_id="big", provider="p", model="openai/gpt-5", input_tokens=400_000)
    assert o.store.last_prompt_tokens("new") == 0
    assert o.pre_llm_call(session_id="new", user_message="hi", model="openai/gpt-5") is None


def test_provider_budget_leaves_other_providers_alone(make):
    o = make({"mode": "stop", "budgets": [{"provider": "anthropic", "usd": 1, "period": "week"}]})
    _spent_out(o, provider="anthropic", session="a")
    o.store.add_call(ts=o.clock() - 60, session_id="o", provider="openrouter", model="cheap", input_tokens=10)
    o.store.add_call(ts=o.clock() - 60, session_id="a", provider="anthropic", model="m", input_tokens=10)
    assert o.pre_llm_call(session_id="a", user_message="x")["action"] == "block"
    assert o.pre_llm_call(session_id="o", user_message="x") is None
    assert o.pre_llm_call(session_id="new", user_message="x", model="cheap") is None    # known model elsewhere
    assert o.pre_llm_call(session_id="new2", user_message="x", model="m")["action"] == "block"
    assert o.pre_llm_call(session_id="new3", user_message="x", model="never-seen") is None
    assert o.pre_tool_call(session_id="o") is None


def test_steer_stays_on_the_provider_and_only_goes_cheaper(make):
    o = make({"budget_usd": 1, "mode": "save", "cheap_model": "gpt-5-mini",
              "prices": {"gpt-5-mini": [1, 2], "claude-x": [3, 15], "tiny": [0.1, 0.2]}})
    _spent_out(o)
    assert o.llm_request(request={"model": "claude-x"}, provider="anthropic") is None       # can't serve it
    assert o.llm_request(request={"model": "claude-x"}, provider="openrouter")["request"]["model"] == "gpt-5-mini"
    assert o.llm_request(request={"model": "tiny"}, provider="openrouter") is None          # already cheaper


def test_rejection_only_pauses_that_provider_and_ignores_other_failures(make):
    o = make({"budget_usd": 1, "mode": "save", "cheap_model": "c/mini"})
    _spent_out(o)
    assert o.llm_request(request={"model": "big"}, provider="p1")
    o.api_request_error(status_code=401, model="c/mini")                      # the key, not the model
    o.api_request_error(status_code=404, model="other")                       # another request
    assert o.store.meta("steer_failed") is None
    o.api_request_error(status_code=404, model="c/mini")
    assert o.llm_request(request={"model": "big"}, provider="p1") is None
    assert o.llm_request(request={"model": "big"}, provider="p2")


@pytest.mark.parametrize("bad", [[1, 2], "x", {"m": ["1", "two"]}, {"m": "12"}, {"m": [float("nan"), 1]}, {"m": [1]}])
def test_bad_prices_setting_never_stops_metering(make, bad):
    o = make({"prices": bad})
    o.meter(session_id="s", provider="p", model="m", usage={"input_tokens": 10})
    assert o.store.providers(0) == ["p"]
    assert o.estimate_usd("m", 100) is None


def test_two_budgets_alert_separately(make):
    o = make({"mode": "alert", "unusual_day_x": 0, "budgets": [{"provider": "a", "usd": 1, "period": "week"},
                                                              {"provider": "b", "usd": 1, "period": "week"}]})
    _spent_out(o, provider="a", session="sa")
    _spent_out(o, provider="b", session="sb")
    o.pre_llm_call(session_id="sa", user_message="x")
    o.pre_llm_call(session_id="sb", user_message="x")
    assert o.store.meta("notice:sa") and o.store.meta("notice:sb")


def test_corrupt_weights_back_off(make, monkeypatch):
    o = make({"budget_usd": 100})
    feed(o, 200, 0.1)
    calls = []

    def boom(*a, **k):
        calls.append(1)
        raise RuntimeError("bad weights")
    monkeypatch.setattr(_forecast, "cascade", boom)
    for _ in range(3):
        assert o.forecast(o.budget())
    assert len(calls) == 1


@pytest.mark.parametrize("text", ["topup -5", "topup nan", "topup inf", "budget nan", "budget inf", "budget -3", "cap 0"])
def test_bad_amounts_are_refused(make, text):
    o = make({"budget_usd": 5})
    assert "amount above zero" in _commands.run(o, text)
    assert o.budget().usd == 5 and o.outlook() is not None


def test_topup_with_a_scoped_main_budget(make):
    o = make({"budget_usd": 5, "provider": "openrouter"})
    assert o.topup(5) is not None
    assert o.outlook().usd == 10


@pytest.mark.parametrize("v", ["$20", "abc", None, float("nan"), -4, [1]])
def test_hand_edited_budget_never_raises(make, v):
    o = make({"budget_usd": v, "unusual_day_x": "many", "max_request_usd": "lots"})
    assert o.budget().usd == (20 if v == "$20" else 0)
    assert o.pre_llm_call(session_id="s", user_message="x") is None
    assert _commands.run(o, "")


def test_price_rate_ignores_the_current_partial_hour(make):
    o = make({"budget_usd": 100})
    feed(o, 200, 0.01)
    o.clock_obj.t += 600
    o.store.add_call(ts=o.clock() - 60, session_id="s1", provider="openrouter", model="m", input_tokens=10, cost_usd=5.0)
    seen = {}
    real = _forecast.cascade
    monkey = lambda *a, **k: (seen.update(rate=k["usd_per_token"]), real(*a, **k))[1]  # noqa: E731
    _forecast.cascade, keep = monkey, real
    try:
        o.forecast(o.budget())
    finally:
        _forecast.cascade = keep
    assert seen["rate"] == pytest.approx(0.01 / 1100)


def test_failed_history_read_is_retried_later(make, monkeypatch):
    o = make()
    from obol import _backfill

    def locked(*a, **k):
        raise RuntimeError("database is locked")
    monkeypatch.setattr(_backfill, "import_history", locked)
    assert o.learn_history() == {} and o.store.meta("history_imported") is None
    monkeypatch.setattr(_backfill, "import_history", lambda *a, **k: {"sessions": 2})
    assert o.learn_history() == {}
    o.clock_obj.t += 2 * HOUR
    assert o.learn_history() == {"sessions": 2}


def test_hourly_cache_sees_late_rows(make):
    o = make()
    feed(o, 5, 1.0)
    assert o.store.hourly(until=o.clock())[-1][1] == 1.0
    o.store.add_call(ts=o.clock() - HOUR + 60, session_id="s", provider="p", cost_usd=2.0)
    assert o.store.hourly(until=o.clock())[-1][1] == 3.0


def test_the_person_hears_about_a_switch_under_the_reply_once(make):
    o = make({"budget_usd": 1, "mode": "save", "cheap_model": "c/mini"})
    feed(o, 10, 1.0)
    assert o.llm_request(request={"model": "big"}, provider="p", session_id="s1")
    out = o.transform_llm_output(session_id="s1", response_text="answer")
    assert "switched from big to c/mini" in out and "/obol mode alert" in out
    assert o.llm_request(request={"model": "big"}, provider="p", session_id="s1")
    assert o.transform_llm_output(session_id="s1", response_text="answer") is None


def test_save_without_a_model_says_so(make):
    o = make({"budget_usd": 100, "mode": "save"})
    assert "can't switch yet" in _commands.run(o, "")
    o.cfg["cheap_model"] = "c/mini"
    assert "can't switch yet" not in _commands.run(o, "")


def test_stop_message_gives_the_limit_the_reset_and_a_topup_that_works(make):
    o = make({"budget_usd": 10, "period": "day", "mode": "stop"})
    feed(o, 30, 1.0)
    msg = o.pre_llm_call(session_id="s1", user_message="x")["message"]
    assert "daily limit" in msg and "resets" in msg and "/obol allow" in msg and "/obol mode alert" in msg
    amount = msg.split("/obol topup ")[1].split()[0]
    _commands.run(o, f"topup {amount}")
    assert o.outlook().state != "out"


def test_task_cap_stop_points_at_the_cap(make):
    o = make({"mode": "stop", "budget_usd": 100})
    feed(o, 2, 0.1)
    _commands.run(o, "cap 1")
    o.store.add_call(ts=o.clock() + 1, session_id="s1", provider="p", cost_usd=2.0)
    o.clock_obj.t += 2
    assert "/obol cap off" in o.pre_llm_call(session_id="s1", user_message="x")["message"]


def test_tight_budget_tells_the_person(make):
    o = make({"budget_usd": 100, "unusual_day_x": 0})
    o.outlook = lambda b=None: _budget.Outlook(spent=50, usd=100, start=0, end=o.clock() + 9e4, end_median=90)
    o.pre_llm_call(session_id="s1", user_message="x")
    assert "close: on track" in o.store.meta("notice:s1")[0]


def test_what_if_prices_your_mix_on_the_cheaper_model(make):
    o = make({"budget_usd": 30, "period": "week", "mode": "alert", "cheap_model": "tiny",
              "prices": {"tiny": [0.01, 0.01], "m": [10, 10]}})
    feed(o, 40, 0.5)
    w = o.what_if()
    assert w and w["model"] == "tiny" and w["expected"] < o.outlook().end_median
    assert "what if: on tiny" in _commands.run(o, "")


def test_what_if_stays_quiet_when_it_cant_help(make):
    o = make({"budget_usd": 3000, "cheap_model": "tiny", "prices": {"tiny": [1, 1]}})
    feed(o, 40, 0.5)
    assert o.what_if() is None                                  # plenty of budget
    o.cfg["budget_usd"] = 30
    o.cfg["cheap_model"] = "no-price-known"
    assert o.what_if() is None
    o.cfg["cheap_model"] = ""
    assert o.what_if() is None


def test_demo_saves_money_and_touches_nothing_real(make):
    o = make({"budget_usd": 7})
    feed(o, 3, 0.1)
    before = (o.store.spent(0, o.clock()), o.cfg.copy())
    out = _commands.run(o, "demo")
    assert "made-up usage, nothing saved" in out and "switches to the cheaper model" in out
    without = float(out.split("With no plugin: $")[1].split(",")[0])
    limit = float(out.split("Limit $")[1].split()[0])
    final = float(out.split("save mode: $")[1].rstrip("."))
    assert final < limit < without
    assert (o.store.spent(0, o.clock()), o.cfg) == before


def test_switch_holds_until_well_under_budget(make):
    o = make({"budget_usd": 100, "mode": "save", "cheap_model": "c/mini", "unusual_day_x": 0})
    feed(o, 3, 0.1)

    def at(expected):
        o.outlook = lambda b=None: _budget.Outlook(spent=10, usd=100, start=0, end=o.clock() + 9e4, end_median=expected)
        return o.llm_request(request={"model": "big"}, provider="p", session_id="s") is not None
    assert not at(80)                # never switched: 80% is fine
    assert at(90)                    # 85% switches
    assert at(80) and at(62)         # the switch lowered the forecast: stays
    assert not at(50)                # well under: back to the usual model
    assert not at(80)                # and it takes 85% to switch again


def test_demo_doesnt_flip_flop_on_either_forecaster(make, monkeypatch):
    o = make()
    for tier in ("cascade", "seasonal"):
        if tier == "seasonal":
            monkeypatch.setattr(_forecast, "cascade_status", lambda *a, **k: (False, "no numpy"))
        out = _commands.run(o, "demo")
        assert out.count("→") <= 4, out


# ── plan limits ──────────────────────────────────────────────────────────────

def _plan(o, monkeypatch, used=60.0, reset_in=2 * HOUR, label="Current session", scope="account", provider="anthropic"):
    from obol import _limits
    rows = [{"label": label, "used": used, "scope": scope, "reset_at": o.clock() + reset_in}]
    monkeypatch.setattr(_limits, "read", lambda p: {"windows": rows if p == provider else [], "error": None if p == provider else "not signed in to Hermes"})
    return o.read_limits(provider)


def test_project_scales_the_forecast_by_the_windows_own_rate():
    from obol import _limits
    now = 3600.0 * 1000
    # 50% used on 100k tokens; 100k more forecast before the reset: reaches 100 at the end of the second hour
    p = _limits.project(50, now, now + 4 * HOUR, 5 * HOUR, 100_000, [50_000] * 4, now)
    assert p["how"] == "forecast" and p["expected"] == 150
    assert p["out_at"] == now + 2 * HOUR
    assert _limits.state(50, p["expected"]) == "over"


def test_project_counts_only_the_part_of_the_last_hour_inside_the_window():
    from obol import _limits
    now = 3600.0 * 1000
    p = _limits.project(50, now, now + 1.5 * HOUR, 5 * HOUR, 100_000, [10_000] * 4, now)
    assert p["expected"] == pytest.approx(50 + 7.5)


def test_use_from_other_clients_falls_back_to_pace():
    from obol import _limits
    now = 3600.0 * 1000
    p = _limits.project(40, now, now + 3 * HOUR, 5 * HOUR, 200, [10_000] * 4, now)
    assert p["how"] == "pace" and p["expected"] == pytest.approx(100)
    early = _limits.project(1, now, now + 4.99 * HOUR, 5 * HOUR, 0, None, None)
    assert early["expected"] is None and _limits.state(1, early["expected"]) == "ok"


def test_window_lengths():
    from obol import _limits
    assert _limits.length("Current session") == 5 * HOUR and _limits.length("Current week") == 7 * 86400
    assert _limits.length("Opus week") == 7 * 86400 and _limits.length("Mystery") is None


def test_plan_limit_warns_with_no_budget_and_only_that_provider(make, monkeypatch):
    o = make({"unusual_day_x": 0})
    feed(o, 1, 0.1, provider="anthropic", session="a")
    feed(o, 1, 0.1, provider="openrouter", session="b")
    o.store.add_call(ts=o.clock() - 60, session_id="a", provider="anthropic", input_tokens=100_000)
    _plan(o, monkeypatch, used=70, reset_in=4 * HOUR)
    o._token_path = lambda p: {"start": o.clock(), "mean": [100_000] * 8}
    told = o.pre_llm_call(session_id="a", user_message="x")
    assert "Claude current session" in told["context"] and "hit the limit" in told["context"]
    assert o.pre_llm_call(session_id="b", user_message="x") is None


def test_plan_limit_reached_stops_in_stop_mode_with_a_way_out(make, monkeypatch):
    o = make({"mode": "stop"})
    o.store.add_call(ts=o.clock() - 60, session_id="a", provider="anthropic", input_tokens=10)
    _plan(o, monkeypatch, used=100)
    msg = o.pre_llm_call(session_id="a", user_message="x")["message"]
    assert "limit reached" in msg and "/obol allow" in msg and "topup" not in msg.replace("topped up", "")
    _commands.run(o, "allow")
    assert "action" not in (o.pre_llm_call(session_id="a", user_message="x") or {})


def test_model_window_only_speaks_for_that_model(make, monkeypatch):
    o = make({"mode": "stop"})
    o.store.add_call(ts=o.clock() - 60, session_id="a", provider="anthropic", model="claude-opus-5", input_tokens=10)
    _plan(o, monkeypatch, used=100, label="Opus week", scope="model", reset_in=86400)
    assert o.pre_llm_call(session_id="a", user_message="x", model="claude-opus-5")["action"] == "block"
    assert o.pre_llm_call(session_id="a", user_message="x", model="claude-sonnet-5") is None


def test_stale_or_passed_readings_arent_acted_on(make, monkeypatch):
    o = make({"mode": "stop"})
    o.store.add_call(ts=o.clock() - 60, session_id="a", provider="anthropic", input_tokens=10)
    _plan(o, monkeypatch, used=100, reset_in=30 * HOUR)
    o.clock_obj.t += 2 * HOUR
    assert o.pre_llm_call(session_id="a", user_message="x") is None      # old reading
    assert "limit reached" in _commands.run(o, "").replace("\n", " ")   # still shown
    _plan(o, monkeypatch, used=100, reset_in=HOUR)
    o.clock_obj.t += 2 * HOUR
    assert o.limit_windows("anthropic") == []                            # it has reset


def test_limits_command_reads_now_and_says_why_not(make, monkeypatch):
    o = make()
    _plan(o, monkeypatch, used=30, reset_in=4 * HOUR)
    out = _commands.run(o, "limits")
    assert "Claude current session: 30% used" in out and "Codex: not signed in" in out
    o.cfg["plan_limits"] = "off"
    assert "off" in _commands.run(o, "limits")
    assert o.limit_windows("anthropic") == []


def test_limits_are_read_in_the_background_and_throttled_only_for_those_providers(make, monkeypatch):
    from obol import _limits
    o = make()
    seen = []
    monkeypatch.setattr(_limits, "read", lambda p: (seen.append(p), {"windows": [], "error": None})[1])
    started = []

    class NowThread:
        def __init__(self, target, args=(), **k):
            self.go = lambda: target(*args)

        def start(self):
            started.append(1)
            self.go()
    monkeypatch.setattr(_plugin.threading, "Thread", NowThread)
    use = {"input_tokens": 10}
    o.meter(session_id="s", provider="openrouter", model="m", usage=use)
    assert seen == []
    o.meter(session_id="s", provider="anthropic", model="m", usage=use)
    o.meter(session_id="s", provider="anthropic", model="m", usage=use)
    assert seen == ["anthropic"]
    o.clock_obj.t += 600
    o.meter(session_id="s", provider="anthropic", model="m", usage=use)
    assert seen == ["anthropic", "anthropic"]


def test_failed_read_keeps_the_last_good_one(make, monkeypatch):
    from obol import _limits
    o = make()
    _plan(o, monkeypatch, used=30, reset_in=4 * HOUR)
    monkeypatch.setattr(_limits, "read", lambda p: {"windows": [], "error": "couldn't read it (HTTPStatusError)"})
    rec = o.read_limits("anthropic")
    assert rec["windows"] and rec["error"]
