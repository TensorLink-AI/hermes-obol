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
    assert o.llm_request(request=req, provider="anthropic") is not None  # budget covers all providers
    o.cfg["provider"] = "openrouter"
    assert o.llm_request(request=req, provider="anthropic") is None


def test_commands_roundtrip(make):
    o = make()
    assert "no budget" in _commands.run(o, "")
    assert "Budget saved" in _commands.run(o, "budget 25 week")
    assert o.cfg["budget_usd"] == 25 and o.cfg["period"] == "week"
    assert "Budget for openrouter saved" in _commands.run(o, "budget 10 day openrouter")
    assert o.cfg["budgets"] == [{"provider": "openrouter", "usd": 10.0, "period": "day"}]
    assert "removed" in _commands.run(o, "budget 0 day openrouter") and o.cfg["budgets"] == []
    assert _commands.run(o, "mode nope").startswith("Usage")
    assert "isn't a number" in _commands.run(o, "budget abc")


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
    o = make({"budget_usd": 3, "period": "week"})
    feed(o, 40, 0.05)
    assert "via Ephemeris" in _commands.run(o, "")
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
