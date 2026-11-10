# Obol

**Know when you're going to run out of tokens, before it happens.**

Obol forecasts your token use hour by hour, from how you actually work rather than a straight line, and tells you when
your budget or your Claude or Codex plan limit runs out. It can warn you, switch to a cheaper model, or stop at the limit.

Needs Hermes 0.22.7 or later. The local forecast needs numpy; without it Obol uses its built-in seasonal forecast.

```sh
hermes plugins install TensorLink-AI/hermes-obol
hermes plugins enable obol
```

Restart Hermes (and its local gateway, `hermes gateway restart`). In Hermes Desktop, also turn on the status-bar chip
under **Capabilities → Plugins → Obol → Desktop**. Then in any session:

```
/obol                    what you've spent, what's usual, and a budget to try
/obol demo               watch it work on a made-up week (nothing saved)
/obol budget 20 week     set a limit
/obol mode save          switch to a cheaper model when it's getting close
/obol cheap deepseek/deepseek-v4-flash
/obol topup 5            add $5 to this period
/obol pause 2h           take a break from Obol acting
/obol why                what this is, and what Obol did lately
/obol more               everything else
```

The forecast comes from a small model that runs on your CPU, offline. It learns from your Hermes history when you
install it, so it works from day one, and it acts while there's still budget left. Budgets are in US dollars, for API
usage, worked out from list prices, so they can differ from your provider's bill. Days, weeks (from Monday) and months
are calendar periods in your local time. On Claude or Codex plans it forecasts the plan's own limits, and plan requests
count as $0 against any budget. With under a day of history it says it's too early rather than guess. Times are rough:
Obol rounds them down to the quarter hour, or the hour when they're further off.

```
/obol
Obol · $6.20 of $20 · ⚠ on track to run out ~Fri 14:00
  weekly budget · save mode (switches to deepseek/deepseek-v4-flash)
  forecast: Cascade toktok 4M (local, CPU)
  last 48h  ▁▁▂▅▇▃▁▁▁▁▂▆█▅▂▁
```

## What happens when the limit gets close

Obol acts once the forecast reaches 85% of the limit.

| Mode | What Obol does |
|---|---|
| `alert` (default) | Tells you, under the agent's reply (in chat apps, straight away as its own message), and tells the agent, so it keeps answers short and asks before long work. |
| `save` | Also switches to a cheaper model on the same provider, at that point. The first switch of a day waits one turn and warns you under that reply, so you know before answers change. Works on OpenRouter and similar routers, and on Anthropic, OpenAI, Codex, DeepSeek, Gemini and xAI when the model is theirs. On any other provider it never switches. |
| `stop` | Also stops new turns, and tool calls inside a running turn, once the budget is used up, never on a forecast alone. `/obol allow` lets your next message through (send it again after). To stop earlier, write a rule with `when: {state: over}` and `do: block`. |

`/obol demo` plays a made-up heavy week through the real forecast and rules, in a temporary ledger, so you can see
what it does before you have any history:

```
/obol demo
Obol demo · made-up usage, nothing saved. Limit $15 a week, a usual week is about $12, this one is 1.7× as heavy.
  Mon 12:00  $1.64 of $15.00 · ⚠ on track to run out ~Fri 10:00
  Mon 15:00  $2.96 of $15.00 · ⚠ on track to run out ~Thu 15:00  → switches to the cheaper model
  Wed 15:00  $3.29 of $15.00 · on track for $8.86 this week  → back to the usual model
  Fri 12:00  $10.24 of $15.00 · on track for $12.28 this week
  Obol saw it coming Mon 12:00, with $1.64 of $15 spent. A meter that warns at 80% would have waited until Wed 16:10.
  With no plugin: $20.84, $5.84 over the limit.
  With Obol in save mode: $12.97.
```

(Shortened; the reply also draws the week.) When a budget is short and a cheaper model is set, `/obol` also shows what
switching would do, priced on your own recent token mix.

Every setting is also on the plugin's page under Desktop → Settings → Plugins → Obol. The agent can check the budget itself
with the `obol_budget` tool. After upgrading, restart Hermes and its gateway so the new code loads.

## Hermes Desktop

A chip in the status bar shows whichever budget or plan limit is closest to running out, like `$6.20/$20 · out ~Fri 14:00`
or `Claude week 41%`.
Click it for the chart: use so far, the forecast, and when it crosses the limit, for a budget or a Claude or Codex window. It shows your weekly limit when you have one, unless a budget you set is the most urgent.
The shaded range is how much your own past periods varied, and appears once there are four to measure. If a limit is about
to go, it also shows a notification. `/obol demo` puts a made-up heavy week on the chip for 5 minutes, labelled as a demo.

Desktop keeps a plugin's Desktop half off until you choose, which is the switch in the install steps above. Installing
from that page with "Enable agent plugin after install" does this for you.

## Turning it off

`/obol budget off` removes the limit, `/obol pause 2h` takes a break, `hermes plugins disable obol` turns it off and
`hermes plugins remove obol` uninstalls it. Obol's history stays on your machine after removal, so a reinstall
picks up where you left off. To delete it, remove Obol's folder under `plugin-data/` in your Hermes home
(`agent-plugin-obol-*`, holding `obol.db`, `desktop.json` and `prices.json`).

## Claude and Codex plans

If you're signed in to Hermes with a Claude or Codex plan, `/obol limits` shows each window and whether it fills
before it resets. No budget or setup needed, and the alerts and modes above apply to these too:

```
/obol limits
Plan limits (the vendor's figures, across every client on the account):
  Claude current session: 62% used · ⚠ on track to hit the limit ~15:20, before it resets 16:00
  Claude current week: 41% used · on track for 73% before it resets Mon 09:00
```

The percentages come from the vendor, so they include your use outside Hermes. The projection scales the token
forecast by how much of the window each Hermes token has used so far. Obol checks that against its readings: when
Hermes was busy and the percent barely moved, most of the use came from somewhere else, so it uses your pace so far and
says so. Until the readings can tell, it shows whichever estimate is higher. It's a forecast, so treat the time as an
early warning. Obol goes through whichever
sign-in Hermes resolves, which can include a Claude Code login Hermes finds on the machine. Obol never touches it itself.

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

Modes (alert, save, stop) are presets of rules. For full control, set `rules` (Desktop → Settings → Plugins → Obol, or under
`plugins.entries.obol.settings` in `config.yaml`). Your rules replace the mode rather than adding to it, and every rule
that matches fires. Rules are plain data, never code:

```yaml
plugins:
  entries:
    obol:
      settings:
        rules:
          - name: unusual day
            when: {day_vs_usual: ">3x"}            # today on track for 3x your usual day
            do: notify                             # a line under the next reply (CLI, Telegram, Desktop)
          - when: {state: [tight, over], provider: openrouter}
            do: {steer: [anthropic/claude-sonnet-5.5, deepseek/deepseek-v4-flash]}   # a ladder: harsher, cheaper
          - when: {request_usd: ">0.50"}           # this turn's input alone would cost over $0.50
            do: ask                                # held: /obol allow, then send it again
          - when: {state: out}
            do: block
```

Conditions: `state` (ok, tight, over, out), `runs_out_within` (`2h`), `spent_pct`, `request_usd`,
`day_vs_usual`, `provider` / `model` / `platform` (globs). Actions: `note` (tell the agent),
`notify` (tell you), `steer`, `ask`, `block`. A rule with a mistake in it doesn't run, so check yours with `/obol rules`:
it marks any condition that can never match, and counts rules it had to ignore.

The Desktop settings field takes the same rules as JSON:

```json
[{"name": "unusual day", "when": {"day_vs_usual": ">3x"}, "do": "notify"},
 {"when": {"state": "out"}, "do": "block"}]
```

Day to day: `/obol topup 5` adds to this period, `/obol allow` lets your next held message through, `/obol pause 2h`
stops acting (metering continues), `/obol cap 2` stops everything after $2 more, across all sessions, until `/obol cap off`, and `/obol why` shows
what Obol did and why.

On a shared gateway, anyone Hermes lets use the bot can run these commands, including `/obol topup` and `/obol allow`:
Obol has no owner check of its own. The agent's `obol_budget` tool only reads.

The name: an obol was the small coin paid for the crossing that Hermes guided. A small coin for a token.

## Forecasting runs on your machine

Obol uses the best forecaster this machine can run, and you never need an account:

1. **Ephemeris** (optional): a hosted panel of larger time-series foundation models (Chronos-2, TimesFM 2.5,
   Toto 2 and others). Used only when you save an Ephemeris API key and run `/obol forecaster ephemeris`. Ephemeris is a
   paid service run by TensorLink, the makers of Obol; new accounts get a small free credit. Without a key,
   `/obol` shows one line saying it exists. Nothing is ever added to the agent's context about it.
2. **Cascade toktok 4M** (the default): [Cascade-toktok-4m-v1](https://huggingface.co/Tensor-Link/cascade-toktok-4m-v1),
   a 3.3M-parameter Toto2-architecture model fine-tuned on hourly LLM token counts from coding agents and
   chat apps. Obol forecasts your **tokens** per hour and converts to dollars at your blended price over the
   last 7 days. Past 24 hours it averages in the base Cascade 4M model, which helps weekly totals. Both
   ship with the plugin at half precision (13 MB together) and run in plain numpy on the CPU: no GPU, no
   torch, no download, no account, about 20 ms a forecast. The port matches the original PyTorch models to
   within 0.25% (the test holds it to that). It needs 128 hours of history, which the history import usually provides on install.
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

**Against a usage meter.** The simple way to project from a meter is a straight line from what you've used so far. In
a separate replay over 13 weeks of one heavy user's hourly Claude Code logs, for week, day and session totals:

| | Error (WAPE, lower is better) |
|---|---|
| **Cascade (Obol)** | **0.57–0.60** |
| 14-day average | 0.69–0.82 |
| Straight line from the usage so far | 0.68–0.89 |

That's lower error than either, but still large: about 60%, and it runs 9–18% low. The figure is higher than the 40%
above because it weighs every window by its size, so big misses count for more. Over a whole week the gain over the straight
line is uncertain, and Cascade isn't clearly better than Obol's built-in seasonal fallback. Again one user, possibly
overlapping the training data. That usage log is private, so neither table can be re-run from this repo; the
model-match test in `tests/` can.

## Disclosure

**Desktop.** The plugin writes a small `desktop.json` to its own data folder with the figures the chip shows. Its backend route, `/api/plugins/obol/status`, only reads that file, and the chip makes no other requests.

**Plan limits.** While you use a Claude or Codex plan, at most every 5 minutes, Obol asks Hermes for that login's limits with `agent.account_usage.fetch_account_usage(read_only=True)`. `/obol limits` and your first `/obol` ask about both providers. Hermes makes the request to the vendor with the credential it resolves, and for Claude it may refresh that login itself; Obol never sees the credential, and stores only the window names, percentages and reset times. Turn it off with the `plan_limits` setting.

- **What it reads:** the token counts and model of each provider request, from Hermes's
  `post_api_request` and `post_auxiliary_call` hooks. To estimate a request's size before it goes, it counts the
  length of the message text in memory; it never stores that text or sends it anywhere. Cost comes from
  your `prices` setting, OpenRouter's public price list, or a few built-in prices. Requests with no price count
  as $0 and are reported in `/obol`. Plan requests, `:free` models and local endpoints cost $0.
- **Past usage (once):** on first use, Obol reads the active profile's `state.db` read-only to learn how
  you have used Hermes: each session's time span, model, billing provider, token counts and cost, plus the
  timestamps of its assistant replies. It never reads message text. Only time before Obol's own first
  request is imported (up to 90 days), so nothing is counted twice, and forecasts start right away
  instead of after a day of use.
- **What it stores:** one row per request (time, session id, platform, provider, model, token counts,
  cost) in `obol.db` in this plugin's data directory under the active profile. The same file also holds Obol's own
  state: your plan-limit readings, cached forecasts, a short log of what Obol did and why (model names and budget
  figures), notices waiting to be shown, and the last forecast or provider error. `desktop.json` holds the chip's
  figures and `prices.json` the public price list. No prompts, replies or credentials.
- **What it changes:** in `stop` mode, `pre_llm_call` refuses new turns and `pre_tool_call` refuses tool
  calls once the budget is used up. In `save` mode, the `llm_request` middleware replaces only the `model`
  field of the request with your `cheap_model` (main-loop requests; auxiliary calls such as titles and
  compression are metered but not steered). If the provider refuses the cheap model, steering to it pauses
  for 6 hours. `alert` mode only adds context and a line under replies. If a stop is in force and `obol.db` can't be read, Obol stops the turn rather than letting it through unchecked. A `/obol cap` stops turns and tool calls in every
  mode. Obol never approves anything itself, and no Hermes code is patched.
- **Network:** these requests, and no others:
  - Plan limits, through Hermes, as described above. Turn them off with the `plan_limits` setting.
  - With an Ephemeris key saved and `forecaster: ephemeris` chosen, at most one forecast per budget per hour goes to
    `https://ephemeris.cascade.industries/api/v1/forecast`. It carries only the hourly dollar totals, with
    no prompts, model names, sessions or identifiers, and it spends Ephemeris credits. No automatic retries;
    after a failure Obol stays local for 3 hours.
  - Prices: at most once a day (hourly while the fetch is failing), a GET of OpenRouter's public model list
    (`openrouter.ai/api/v1/models`), with no key and nothing about you sent. Turn it off with the `price_list` setting.
- **Background work:** the local Cascade forecast takes about 20 ms and runs inside a hook. Three kinds of short-lived
  background thread do the network requests above: Ephemeris forecasts, plan-limit reads and the price list. One
  long-lived timer thread rewrites `desktop.json` every 30 seconds from saved data. No subprocesses, no telemetry.
- **Credentials:** the optional Ephemeris key is a `secret` setting, read with
  `agent.secret_scope.get_secret`. Obol never reads `.env`, `auth.json` or any other tool's credentials.


## Development

```sh
python -m pytest            # no network, no model
hermes plugins validate . --install-deps
```

Apache-2.0 licensed. Built by [TensorLink](https://github.com/TensorLink-AI).
