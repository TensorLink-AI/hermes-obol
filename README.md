# Obol

**Know when you're going to run out of tokens, before it happens.**

Obol forecasts your token use hour by hour, from how you actually work rather than a straight line, and tells you when
your budget or your Claude or Codex plan limit runs out. It can warn you, switch to a cheaper model, or stop at the limit.

Needs Hermes 0.22.7 or later, and numpy for the local forecast (without it, a built-in seasonal forecast).

```sh
hermes plugins install obol          # or: hermes plugins install TensorLink-AI/hermes-obol
hermes plugins enable obol
hermes gateway restart
```

In Hermes Desktop, also turn on the status-bar chip under **Capabilities → Plugins → Obol → Desktop**. Then in any session:

```
/obol                    what you've spent, what's usual, and a budget to try
/obol demo               watch it work on a made-up week (nothing saved)
/obol budget 20 week     set a limit
/obol mode save          switch to a cheaper model when it's getting close
/obol cheap deepseek/deepseek-v4-flash
/obol limits             your Claude or Codex plan windows
/obol why                what Obol did lately, and why
/obol more               everything else
```

```
/obol
Obol · $6.20 of $20 · ⚠ on track to run out ~Fri 14:00
  weekly budget · save mode (switches to deepseek/deepseek-v4-flash)
  forecast: Cascade toktok 4M (local, CPU)
  last 48h  ▁▁▂▅▇▃▁▁▁▁▂▆█▅▂▁
```

The forecast is a small model that runs on your CPU, offline. It learns from your Hermes history on install, so it
works from day one. Budgets are US dollars at list prices (your bill can differ), over calendar days, weeks (from
Monday) or months in local time. Plan requests count as $0 against a budget. Times are rounded down to the quarter
hour, or the hour when further off. With under a day of history Obol says it's too early rather than guess.

## When the limit gets close

Obol acts once the forecast reaches 85% of the limit.

| Mode | What Obol does |
|---|---|
| `alert` (default) | Tells you under the agent's reply (in chat apps, as its own message), and tells the agent to keep answers short and ask before long work. |
| `save` | Also switches to your cheaper model on the same provider. The first switch of a day waits a turn and warns you first. Works on OpenRouter-style routers, and on Anthropic, OpenAI, Codex, DeepSeek, Gemini and xAI for their own models; elsewhere it never switches. |
| `stop` | Also refuses new turns and tool calls once the budget is used up, never on a forecast alone. `/obol allow` lets your next message through. |

```
/obol demo
Obol demo · made-up usage, nothing saved. Limit $15 a week, a usual week is about $12, this one is 1.7× as heavy.
  Mon 12:00  $1.64 of $15.00 · ⚠ on track to run out ~Fri 10:00
  Mon 15:00  $2.96 of $15.00 · ⚠ on track to run out ~Thu 15:00  → switches to the cheaper model
  Wed 15:00  $3.29 of $15.00 · on track for $8.86 this week  → back to the usual model
  Obol saw it coming Mon 12:00, with $1.64 of $15 spent. A meter that warns at 80% would have waited until Wed 16:10.
  With no plugin: $20.84, $5.84 over the limit.
  With Obol in save mode: $12.97.
```

Day to day: `/obol topup 5` adds to this period, `/obol pause 2h` stops acting (metering continues), `/obol cap 2`
stops everything after $2 more until `/obol cap off`. The agent can read the budget with the `obol_budget` tool.

## Claude and Codex plans

Signed in to Hermes with a Claude or Codex plan? `/obol limits` shows each window and whether it fills before it
resets. No budget needed, and the modes above apply.

```
/obol limits
Plan limits (the vendor's figures, across every client on the account):
  Claude current session: 62% used · ⚠ on track to hit the limit ~15:20, before it resets 16:00
  Claude current week: 41% used · on track for 73% before it resets Mon 09:00
```

The percentages are the vendor's, so they include Claude Code, claude.ai and Codex outside Hermes. Obol re-reads them
every 15 minutes. When Hermes was busy but the percent barely moved, most use is coming from elsewhere, so Obol projects
from your pace so far and says so.

## Hermes Desktop

The status-bar chip shows whichever budget or plan limit is closest to running out (`$6.20/$20 · out ~Fri 14:00`,
`Claude week 41%`). Click it for bars (solid: used, faded: forecast to the reset, hatched: where it runs out) and a
chart of use against the limit. A notification fires when a limit is about to go.

## Several APIs and your own rules

`/obol providers` forecasts each API on its own usage at its own price, and `/obol budget 15 week anthropic` sets a
budget for one of them. A turn answers to the strictest budget covering its provider.

Modes are presets of rules. For full control, set `rules` in Desktop settings or `config.yaml`; they replace the mode.
Rules are plain data, never code:

```yaml
plugins:
  entries:
    obol:
      settings:
        rules:
          - when: {day_vs_usual: ">3x"}            # today on track for 3x your usual day
            do: notify
          - when: {state: [tight, over], provider: openrouter}
            do: {steer: [anthropic/claude-sonnet-5.5, deepseek/deepseek-v4-flash]}
          - when: {request_usd: ">0.50"}           # this turn's input alone would cost over $0.50
            do: ask                                # held until /obol allow
          - when: {state: over}
            do: block                              # stop before the budget is gone
```

Conditions: `state` (ok, tight, over, out), `runs_out_within`, `spent_pct`, `request_usd`, `day_vs_usual`, `provider`
/ `model` / `platform` (globs). Actions: `note`, `notify`, `steer`, `ask`, `block`. `/obol rules` checks yours.

On a shared gateway, anyone Hermes lets use the bot can run `/obol` commands, including `topup` and `allow`: Obol has
no owner check of its own.

## Forecasting

1. **Cascade toktok 4M** (default): [Cascade-toktok-4m-v1](https://huggingface.co/Tensor-Link/cascade-toktok-4m-v1), a
   3.3M-parameter model fine-tuned on hourly token counts from coding agents and chat apps, averaged with the base
   Cascade 4M past 24 hours. It forecasts tokens per hour and prices them at your blended rate over the last 7 days.
   Both ship in the plugin (13 MB, half precision, MIT) and run in plain numpy: no GPU, no download, about 20 ms. The
   port matches the PyTorch originals within 0.25% (tested). Needs 128 hours of history, usually covered by the import.
2. **Seasonal** (built in): each hour's range from the same hour over the last 7 days. Used before 128 hours of history
   and without numpy.
3. **Ephemeris** (optional, paid): a hosted panel of larger forecasting models, run by TensorLink, the makers of Obol.
   Used for dollar budgets only when you save a key in settings and run `/obol forecaster ephemeris`.

**How good is it?** In a replay over 13 weeks of one heavy user's hourly Claude Code logs, Cascade had lower error
(WAPE 0.57–0.60) than a 14-day average (0.69–0.82) or a straight line from usage so far (0.68–0.89), for week, day and
session totals. Error is still about 60% and it runs 9–18% low, so treat run-out times as an early warning. Over a
whole week the gain over the straight line is uncertain. That's one user, possibly overlapping the training data, and
the log is private.

## Disclosure

- **What it reads:** each provider request's token counts and model, from the `post_api_request` and
  `post_auxiliary_call` hooks. To size a request before it goes, it measures the message text in memory, never
  storing or sending it. Once, on first use, it reads the active profile's `state.db` read-only for up to 90 days of
  past sessions (time span, model, provider, token counts, cost, reply timestamps; never message text).
- **What it stores:** in its own data folder (`plugin-data/agent-plugin-obol-*`): `obol.db` (one row per request:
  time, session id, platform, provider, model, tokens, cost; plus plan readings, cached forecasts and a log of what it
  did), `desktop.json` (the chip's figures, served read-only at `/api/plugins/obol/status`) and `prices.json`. No
  prompts, replies or credentials.
- **What it changes:** `stop` refuses turns (`pre_llm_call`) and tool calls (`pre_tool_call`) once a budget is used up,
  or a `/obol cap` is reached, and fails closed if `obol.db` can't be read. `save` replaces only the `model` field of
  main-loop requests (`llm_request` middleware); if the provider refuses it, steering pauses for 6 hours. Obol never
  approves anything and patches no Hermes code.
- **Network**, and nothing else:
  - Plan limits via Hermes's `agent.account_usage.fetch_account_usage(read_only=True)`: after plan requests (at most
    every 5 minutes) and every 15 minutes while the gateway runs, for logins that have answered. Hermes uses its own
    credential and may refresh a Claude login; Obol never sees it and stores only window names, percentages and resets.
    Off with `plan_limits`.
  - Prices: a GET of OpenRouter's public model list at most daily (hourly while failing), no key. Off with `price_list`.
  - Ephemeris, only if chosen: at most one request per budget per hour to `ephemeris.cascade.industries`, carrying only
    hourly dollar totals, spending your credits. After a failure Obol stays local for 3 hours.
- **Background work:** short-lived threads for those three requests, and one timer thread that rewrites `desktop.json`
  every 30 seconds and triggers the plan re-reads. No subprocesses, no telemetry.
- **Credentials:** the Ephemeris key is a `secret` setting read with `agent.secret_scope.get_secret`. Obol never reads
  `.env`, `auth.json` or any other tool's credentials.

## Turning it off

`/obol budget off` removes the limit, `hermes plugins disable obol` turns Obol off, `hermes plugins remove obol`
uninstalls it. History stays on your machine; delete the `agent-plugin-obol-*` folder under `plugin-data/` to remove it.

## Development

```sh
python -m pytest            # no network, no model
hermes plugins validate . --install-deps
```

Apache-2.0; the bundled weights are MIT (`models/LICENSE`). Built by [TensorLink](https://github.com/TensorLink-AI).
The name: an obol was the coin paid for the crossing Hermes guided. A small coin for a token.
