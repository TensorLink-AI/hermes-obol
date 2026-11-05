import obol
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
    o = make({"budget_usd": 10, "period": "day", "mode": "guard"})
    feed(o, 30, 1.0)
    out = o.outlook()
    assert out.state == "out"           # $1/h for the hours since midnight exceeds $10
    r = o.pre_llm_call(session_id="s1")
    assert r["action"] == "block" and "used up" in r["message"]
    assert o.pre_tool_call(tool_name="terminal")["action"] == "block"


def test_watch_tells_once_then_stays_quiet(make):
    o = make({"budget_usd": 200, "period": "week", "mode": "watch"})
    feed(o, 40, 2.0)                    # on pace to blow $200 well before Sunday
    first = o.pre_llm_call(session_id="s1")
    assert "context" in first and "Obol budget" in first["context"]
    assert o.pre_llm_call(session_id="s1") is None


def test_steer_swaps_model_only_when_forecast_says_so(make):
    o = make({"budget_usd": 1000, "period": "week", "mode": "steer", "cheap_model": "cheap/mini"})
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
    assert "Budget saved" in _commands.run(o, "budget 25 week openrouter")
    assert o.cfg["budget_usd"] == 25 and o.cfg["provider"] == "openrouter"
    assert _commands.run(o, "mode nope").startswith("Usage")
    assert "isn't a number" in _commands.run(o, "budget abc")


def test_choose_prefers_key_then_local_model(make, tmp_path):
    assert _forecast.choose("auto", True, tmp_path, 100)[0] == "ephemeris"
    assert _forecast.choose("auto", False, tmp_path, 100)[0] == "cascade"
    assert _forecast.choose("seasonal", False, tmp_path, 100)[0] == "seasonal"
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
    o = make({"budget_usd": 1, "period": "week", "mode": "steer", "cheap_model": "openai/gpt-4o-mini"})
    feed(o, 40, 0.5)
    req = {"model": "deepseek/deepseek-v4.1-flash"}
    assert o.llm_request(request=req, provider="openrouter")["request"]["model"] == "openai/gpt-4o-mini"
    o.api_request_error(status_code=404, error={"message": "No allowed providers"})
    assert o.llm_request(request=req, provider="openrouter") is None
    assert "steer paused" in _commands.run(o, "")
    _commands.run(o, "cheap deepseek/deepseek-v4-flash")
    assert o.llm_request(request=req, provider="openrouter")["request"]["model"] == "deepseek/deepseek-v4-flash"


def test_cascade_numpy_matches_torch_reference():
    import json
    import numpy as np
    from pathlib import Path
    from obol import _cascade
    m = _cascade.Cascade4M()
    for case in json.loads((Path(__file__).parent / "cascade_golden.json").read_text()):
        got = m.quantiles(case["history"], case["horizon"])
        want = np.array(case["torch_quantiles"])
        assert got.shape == want.shape
        assert np.abs(got - want).max() <= 1e-4 * (np.abs(want).max() + 1e-6), case["name"]


def test_cascade_is_the_default_local_forecaster(make):
    o = make({"budget_usd": 3, "period": "week"})
    feed(o, 40, 0.05)
    f = o.forecast()
    assert f["tier"] == "cascade" and len(f["quantiles"]["0.5"]) == f["horizon"]
    assert all(v >= 0 for v in f["quantiles"]["0.1"])
    assert o.outlook().state in ("ok", "tight", "over")


def test_ephemeris_hint_only_in_status_and_only_without_key(make):
    o = make({"budget_usd": 3, "period": "week"})
    feed(o, 40, 0.05)
    assert "optional: Ephemeris" in _commands.run(o, "")
    told = o.pre_llm_call(session_id="s1") or {}
    assert "Ephemeris" not in str(told)                 # never pushed into the agent's context
    o._secret = lambda n: "pc_live_x"
    o.store.set_meta("ephemeris_failed_at", o.clock())  # stay local, but the key exists
    assert "optional: Ephemeris" not in _commands.run(o, "")
