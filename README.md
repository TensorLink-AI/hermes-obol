# Obol

**Know when Hermes will run out of budget, and act before it does.**

Every Hermes plugin can count tokens. Obol forecasts them, and it learns your usage from Hermes's own session
history the moment you install it. It meters every provider request, forecasts the
rest of the day, week or month hour by hour, and tells you when, at this pace, the budget runs out. Then it
acts on the forecast, not just on the wall:

Pick what happens when the budget won't last:

| Mode | What Obol does |
|---|---|
| `alert` (default) | Tells you, under the agent's reply, and tells the agent, so it keeps answers short and asks before long work. |
| `save` | Also switches to a cheaper model on the same provider, early, while there is still budget left. |
| `stop` | Also stops new turns, and tool calls inside a running turn, once the budget is used up. |

```
/obol
Obol · $6.20 of $20 · ⚠ on track to run out ~Fri 14:00 (if busy, Thu 21:00)
  weekly budget · save mode (switches to deepseek/deepseek-v4-flash)
  forecast: Cascade 4M (local, CPU)
  last 48h  ▁▁▂▅▇▃▁▁▁▁▂▆█▅▂▁
```

With no budget set, `/obol` still shows what you've spent, what a usual week costs you, where this one is
heading, and a budget to try.

## Your own rules

Modes (alert, save, stop) are presets of rules. For full control, set `rules` (Desktop → Settings → Plugins → Obol, or `config.yaml`).
Rules are plain data, never code:

```yaml
rules:
  - name: unusual day
    when: {day_vs_usual: ">3x"}            # today on track for 3x your usual day
    do: notify                             # a line under the next reply (CLI, Telegram, Desktop)
  - when: {state: [tight, over], provider: openrouter}
    do: {steer: [anthropic/claude-sonnet-5.5, deepseek/deepseek-v4-flash]}   # a ladder: harsher, cheaper
  - when: {request_usd: ">0.50"}           # this turn's input alone would cost over $0.50
    do: ask                                # held until you /obol allow it
  - when: {state: out}
    do: block
```

Conditions: `state` (ok, tight, over, out), `runs_out_within` (`2h`), `spent_pct`, `request_usd`,
`day_vs_usual`, `provider` / `model` / `platform` (globs), `aux`. Actions: `note` (tell the agent),
`notify` (tell you), `steer`, `ask`, `block`. Unknown conditions never match, and malformed rules are
ignored and listed by `/obol rules`.

Day to day: `/obol topup 5` adds to this period, `/obol allow` lets one held turn through, `/obol pause 2h`
stops acting (metering continues), `/obol cap 2` caps the current task at $2 more, and `/obol why` shows
what Obol did and why.

The name: an obol was the small coin paid for the crossing that Hermes guided. A small coin for a token.

## Forecasting runs on your machine

Obol uses the best forecaster this machine can run, and you never need an account:

1. **Ephemeris** (optional): a hosted panel of larger time-series foundation models (Chronos-2, TimesFM 2.5,
   Toto 2 and others). Used only after you save an Ephemeris API key, which is your opt-in. Ephemeris is a
   paid service run by TensorLink, the makers of Obol; new accounts get a small free credit. Without a key,
   `/obol` shows one line saying it exists. Nothing is ever added to the agent's context about it.
2. **Cascade 4M** (the default): a 4M-parameter time-series foundation model, bundled with the plugin
   (13 MB) and run in plain numpy on the CPU: no GPU, no torch, no download, no account. A forecast
   takes about 10–50 ms, so it suits a laptop, a mini PC or a Raspberry Pi-class box. It matches the
   original PyTorch model to within 2e-6.
3. **Seasonal** (built in, pure Python): each future hour's range comes from the same hour of day over the
   last 7 days. It always works.

Forecast ranges are nominal per-hour 10–90% quantiles, not measured coverage. "At forecast pace" adds each
hour's expected spend to what is already spent (means add up; medians don't, and run low on bursty spend). "High pace" adds the hourly 90% values, a deliberately pessimistic
path. It is not a 90% bound on the total.

### How well it forecasts

A backtest on 100 days of one developer's real agent spend (about $11/hour on average, 27% idle hours):
45 daily forecast origins over the last 45 days, each from the history before it.

| | Pinball loss, next 24 h | Pinball loss, next 7 d | 10–90% range coverage (target 80%) | Error in the 24 h total |
|---|---|---|---|---|
| **Cascade 4M** | **2.95** | **3.53** | **82%** | **34%** (bias +6%) |
| Hour-of-day seasonal | 3.70 | 4.03 | 67% | 45% (bias −14%) |
| Yesterday repeated | 5.95 | 7.60 | 7% | 32% (bias ±) |

Cascade has the lowest loss, and its ranges are calibrated. Totals over a week remain hard (about 40% median
error): agent spend is bursty, so treat the run-out time as an early warning, not a promise. This is
one user's data; your mileage will vary.

## Install

```sh
hermes plugins install TensorLink-AI/hermes-obol
hermes plugins enable obol
```

Then in any session:

```
/obol                    how you're doing, and a suggested budget
/obol budget 20 week     set a limit
/obol mode save          switch to a cheaper model when it won't last
/obol cheap deepseek/deepseek-v4-flash
/obol topup 5            add $5 to this period
/obol pause 2h           take a break from Obol acting
/obol more               everything else
```

Every setting is also on the plugin's page under Desktop → Settings → Plugins. The agent can check the
budget itself with the `obol_budget` tool.

## Disclosure

- **What it reads:** the token counts and model of each provider request, from Hermes's
  `post_api_request` and `post_auxiliary_call` hooks. It never reads prompt or reply text. Cost comes from
  Hermes's own price list (`agent.usage_pricing`). Requests Hermes can't price count as $0 and are reported
  in `/obol`.
- **Past usage (once):** on first use, Obol reads the active profile's `state.db` read-only to learn how
  you have used Hermes: each session's time span, model, billing provider, token counts and cost, plus the
  timestamps of its assistant replies. It never reads message text. Only time before Obol's own first
  request is imported (up to 90 days), so nothing is counted twice, and forecasts start right away
  instead of after a day of use.
- **What it stores:** one row per request (time, session id, platform, provider, model, token counts,
  cost) in `obol.db` in this plugin's data directory under the active profile. Nothing else.
- **What it changes:** in `stop` mode, `pre_llm_call` refuses new turns and `pre_tool_call` refuses tool
  calls once the budget is used up. In `save` mode, the `llm_request` middleware replaces only the `model`
  field of the request with your `cheap_model` (main-loop requests; auxiliary calls such as titles and
  compression are metered but not steered). If the provider refuses the cheap model, steering to it pauses
  for 6 hours. `alert` mode only adds context and a line under replies. Nothing is auto-approved,
  and no Hermes code is patched.
- **Network:** none by default.
  - With an Ephemeris key saved, at most one forecast per hour (only after a new hour of data) goes to
    `https://ephemeris.cascade.industries/api/v1/forecast`. It carries only the hourly dollar totals, with
    no prompts, model names, sessions or identifiers, and it spends Ephemeris credits. No automatic retries;
    after a failure Obol stays local for 3 hours.
  - Prices: at most once a day, a GET of OpenRouter's public model list (`openrouter.ai/api/v1/models`),
    with no key and nothing about you sent.
- **Background work:** model forecasts run in a short-lived background thread, never inside a hook. No
  subprocesses, no telemetry.
- **Credentials:** the optional Ephemeris key is a `secret` setting, read with
  `agent.secret_scope.get_secret`. Obol never reads `.env`, `auth.json` or any other tool's credentials.

After upgrading Obol, restart Hermes (and its local gateway, `hermes gateway restart`) so the new code loads.

## Development

```sh
python -m pytest            # no network, no model
hermes plugins validate . --install-deps
```

Apache-2.0 licensed. Built by [TensorLink](https://github.com/TensorLink-AI).
