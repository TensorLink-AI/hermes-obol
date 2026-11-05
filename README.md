# Obol

**Know when Hermes will run out of budget, and act before it does.**

Every Hermes plugin can count tokens. Obol forecasts them. It meters every provider request, forecasts the
rest of the day, week or month hour by hour, and tells you when, at this pace, the budget runs out. Then it
acts on the forecast, not just on the wall:

| Mode | What Obol does |
|---|---|
| `watch` | Adds one short note to the agent's context when the outlook gets worse ("on pace to run out ~Thu 15:00"), so it keeps answers short and asks before long work. |
| `steer` | Also routes requests to a cheaper model on the same provider once the forecast says the budget won't last. It switches early, while there is still budget left. |
| `guard` | Also stops new turns, and tool calls inside a running turn, once the budget is used up. |

```
/obol
Obol · $6.20 of $20 this week · ⚠ out ~Fri 14:00 (high pace Thu 21:00)
  weekly budget on openrouter · mode steer → openai/gpt-5-mini
  forecast: Cascade 4M (local, CPU)
  last 48h  ▁▁▂▅▇▃▁▁▁▁▂▆█▅▂▁
```

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

Forecast ranges are nominal per-hour 10–90% quantiles, not measured coverage. "At forecast pace" adds the
hourly medians to what is already spent. "High pace" adds the hourly 90% values, a deliberately pessimistic
path. It is not a 90% bound on the total.

## Install

```sh
hermes plugins install TensorLink-AI/hermes-obol
hermes plugins enable obol
```

Then in any session:

```
/obol budget 20 week openrouter
/obol mode steer
/obol cheap openai/gpt-5-mini
```

Every setting is also on the plugin's page under Desktop → Settings → Plugins. The agent can check the
budget itself with the `obol_budget` tool.

## Disclosure

- **What it reads:** the token counts and model of each provider request, from Hermes's
  `post_api_request` and `post_auxiliary_call` hooks. It never reads prompt or reply text. Cost comes from
  Hermes's own price list (`agent.usage_pricing`). Requests Hermes can't price count as $0 and are reported
  in `/obol`.
- **What it stores:** one row per request (time, session id, platform, provider, model, token counts,
  cost) in `obol.db` in this plugin's data directory under the active profile. Nothing else.
- **What it changes:** in `guard` mode, `pre_llm_call` refuses new turns and `pre_tool_call` refuses tool
  calls once the budget is used up. In `steer` mode, the `llm_request` middleware replaces only the `model`
  field of the request with your `cheap_model` (main-loop requests; auxiliary calls such as titles and
  compression are metered but not steered). If the provider refuses the cheap model, steering to it pauses
  for 6 hours. `watch` mode only adds context. Nothing is auto-approved,
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
