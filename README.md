# Obol

**Stop surprise bills from Hermes. Obol warns you before your API spend passes a limit you set, and can switch to
a cheaper model or stop at the limit.**

```sh
hermes plugins install TensorLink-AI/hermes-obol
hermes plugins enable obol
```

Restart Hermes (and its local gateway, `hermes gateway restart`), then in any session:

```
/obol                    what you've spent, what's usual, and a budget to try
/obol budget 20 week     set a limit
/obol mode save          switch to a cheaper model when it won't last
/obol cheap deepseek/deepseek-v4-flash
/obol topup 5            add $5 to this period
/obol pause 2h           take a break from Obol acting
/obol more               everything else
```

Other usage plugins count tokens, show subscription quota or stop at a limit. Obol forecasts your API dollars, hour
by hour, with a small model that runs on your CPU, offline. It learns from your Hermes history when you install it,
so it works from day one, and it acts while there's still budget left.

```
/obol
Obol · $6.20 of $20 · ⚠ on track to run out ~Fri 14:00
  weekly budget · save mode (switches to deepseek/deepseek-v4-flash)
  forecast: Cascade toktok 4M (local, CPU)
  last 48h  ▁▁▂▅▇▃▁▁▁▁▂▆█▅▂▁
```

## What happens when the budget won't last

| Mode | What Obol does |
|---|---|
| `alert` (default) | Tells you, under the agent's reply, and tells the agent, so it keeps answers short and asks before long work. |
| `save` | Also switches to a cheaper model on the same provider, early, and tells you under the reply. |
| `stop` | Also stops new turns, and tool calls inside a running turn, once the budget is used up. `/obol allow` sends one anyway. |

Every setting is also on the plugin's page under Desktop → Settings → Plugins. The agent can check the budget itself
with the `obol_budget` tool. After upgrading, restart Hermes and its gateway so the new code loads.

## Turning it off

`/obol budget off` removes the limit, `/obol pause 2h` takes a break, `hermes plugins disable obol` turns it off and
`hermes plugins remove obol` uninstalls it.

## Several APIs

Obol records every request with its provider, and `/obol providers` forecasts each API on its own usage at
its own price ($ per million tokens over the last 7 days). That matters when one provider costs 20 times
another per token:

```
/obol providers
By provider, this week (each forecast on its own usage, at its own price):
  anthropic      $9.80 → $21.40   $2.10/M tok    budget: $9.80 of $15 · ⚠ on track to run out ~Sat 11:00
  openrouter     $0.48 → $1.10    $0.11/M tok
/obol budget 15 week anthropic     a budget for one API, beside the main one (0 removes it)
/obol topup 5 anthropic
```

Each budget is forecast and acted on separately, and a turn answers to the strictest budget that covers the
provider it uses.

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
2. **Cascade toktok 4M** (the default): [Cascade-toktok-4m-v1](https://huggingface.co/Tensor-Link/cascade-toktok-4m-v1),
   a 3.3M-parameter Toto2-architecture model fine-tuned on hourly LLM token counts from coding agents and
   chat apps. Obol forecasts your **tokens** per hour and converts to dollars at your blended price over the
   last 7 days. Past 24 hours it averages in the base Cascade 4M model, which helps weekly totals. Both
   ship with the plugin at half precision (13 MB together) and run in plain numpy on the CPU: no GPU, no
   torch, no download, no account, about 20 ms a forecast. The port matches the original PyTorch models to
   within 0.2%. It needs 128 hours of history, which the history import usually provides on install.
3. **Seasonal** (built in, pure Python): each future hour's range comes from the same hour of day over the
   last 7 days. Used until there are 128 hours of history, and on machines without numpy.

Forecast ranges are nominal per-hour 10–90% quantiles, not measured coverage. "On track for" adds each
remaining hour's expected spend to what is already spent. Means add up; per-hour quantiles don't (the
model forecasts each hour on its own), so Obol never adds up 90% values to make a "worst case" total.

### How well it forecasts

From the model card: on a held-out 28 days of real coding-agent usage, toktok is about 37–40% better than
"same hour yesterday" on CRPS and about 23% better on MASE, and strongest at 24 hours ahead.

A backtest on about 100 days of one developer's hourly token counts (45 daily forecast origins over the
last 45 days, each using only the history before it; CRPS relative to "same hour yesterday" = 1.00, lower
is better):

| | Next 24 h | Next 7 days | Error in the 7-day total |
|---|---|---|---|
| **toktok, then 50/50 with base past 24 h (Obol)** | **0.60** | **0.58** | 42% |
| toktok alone | 0.60 | 0.59 | 44% |
| base Cascade 4M alone | 0.61 | 0.58 | 37% |
| Same hour yesterday | 1.00 | 1.00 | |

Weekly totals stay hard (about 40% median error) because agent usage is bursty, so treat the run-out time
as an early warning, not a promise. This is one user's data, and it may overlap the model's training data.

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


## Development

```sh
python -m pytest            # no network, no model
hermes plugins validate . --install-deps
```

Apache-2.0 licensed. Built by [TensorLink](https://github.com/TensorLink-AI).
