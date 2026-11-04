"""What a request cost, in USD, from data Obol owns (no Hermes internals).

First match wins:
  1. yours: the `prices` setting, {model: [input, output, cache_read]} in $ per million tokens
  2. OpenRouter's public model catalogue (every model it lists, from every provider), fetched at most
     once a day in the background into this plugin's data directory; no key, nothing sent but a GET
  3. built in: a few list prices, for the first run before the catalogue arrives

A model with no price anywhere costs None (reported as unpriced), never a guess. For subscription
providers these are API-equivalent dollars, not what the subscription bills.
"""
from __future__ import annotations

import json
import re
import threading
import time
import urllib.request
from pathlib import Path

CATALOGUE_URL = "https://openrouter.ai/api/v1/models"
REFRESH_SECONDS = 86400

# (input, output, cache_read, cache_write) $ per million
BUILTIN = {
    "claude-opus-5-5": (4.0, 20.0, 0.20, 5.0),
    "claude-sonnet-5-5": (2.0, 10.0, 0.20, 2.5),
    "claude-haiku-5-5": (0.8, 4.0, 0.08, 1.0),
    "claude-sonnet-4": (3.0, 15.0, 0.30, 3.75),
    "claude-haiku-4-5": (1.0, 5.0, 0.10, 1.25),
}


def normalize(model: str | None) -> str:
    """'anthropic/claude-opus-5.5[1m]' -> 'claude-opus-5-5'."""
    m = (model or "").lower().strip().rsplit("/", 1)[-1]
    m = re.sub(r"\[.*?\]$", "", m).split(":", 1)[0]
    return m.replace(".", "-").removeprefix("anthropic-").removeprefix("us-anthropic-")


def _pm(v):
    try:
        return float(v) * 1e6
    except (TypeError, ValueError):
        return None


def parse_catalogue(models: list) -> dict:
    out = {}
    for m in models:
        mid, p = m.get("id") or "", m.get("pricing") or {}
        if ":" in mid:
            continue
        inp, outp = _pm(p.get("prompt")), _pm(p.get("completion"))
        if inp is None or outp is None or (inp == 0 and outp == 0):
            continue
        read, write = _pm(p.get("input_cache_read")), _pm(p.get("input_cache_write"))
        out.setdefault(normalize(mid), (inp, outp, inp if read is None else read, inp if write is None else write))
    return out


def _match(table: dict, m: str):
    if m in table:
        return table[m]
    for k in sorted((k for k in table if m.startswith(k)), key=len, reverse=True):
        return table[k]
    return None


class Prices:
    def __init__(self, data_dir: Path, overrides=lambda: {}):
        self.path = Path(data_dir) / "prices.json"
        self.overrides = overrides
        self._table = None
        self._fetching = False
        self._lock = threading.Lock()

    def _catalogue(self) -> dict:
        if self._table is None:
            try:
                self._table = {k: tuple(v) for k, v in json.loads(self.path.read_text())["rates"].items()}
            except Exception:
                self._table = {}
        return self._table

    def stale(self, now=None) -> bool:
        now = time.time() if now is None else now
        try:
            return now - json.loads(self.path.read_text())["fetched_at"] > REFRESH_SECONDS
        except Exception:
            return True

    def refresh_async(self, on_done=None) -> None:
        """Fetch the catalogue in a background thread (never inside a hook's critical path)."""
        with self._lock:
            if self._fetching:
                return
            self._fetching = True

        def run():
            try:
                req = urllib.request.Request(CATALOGUE_URL, headers={"User-Agent": "hermes-obol"})
                with urllib.request.urlopen(req, timeout=30) as r:
                    rates = parse_catalogue(json.load(r).get("data") or [])
                if rates:
                    self.path.parent.mkdir(parents=True, exist_ok=True)
                    tmp = self.path.with_suffix(".tmp")
                    tmp.write_text(json.dumps({"fetched_at": time.time(), "rates": rates}))
                    tmp.replace(self.path)
                    self._table = {k: tuple(v) for k, v in rates.items()}
                    if on_done:
                        on_done()
            except Exception:
                # offline or the catalogue is unavailable: built-in prices still apply; retried next day
                try:
                    self.path.parent.mkdir(parents=True, exist_ok=True)
                    if not self.path.exists():
                        self.path.write_text(json.dumps({"fetched_at": time.time() - REFRESH_SECONDS + 3600,
                                                         "rates": {}}))
                except Exception:
                    pass
            finally:
                with self._lock:
                    self._fetching = False

        threading.Thread(target=run, name="obol-prices", daemon=True).start()

    def rate(self, model: str | None):
        m = normalize(model)
        if not m:
            return None
        mine = {normalize(k): tuple(v) + (v[0],) * (4 - len(v)) for k, v in (self.overrides() or {}).items()
                if isinstance(v, (list, tuple)) and len(v) >= 2}
        for table in (mine, self._catalogue(), BUILTIN):
            r = _match(table, m)
            if r:
                return r
        return None

    def cost(self, model, usage: dict):
        """(usd or None, status)."""
        r = self.rate(model)
        if r is None:
            return None, "unpriced"
        g = lambda k: int(usage.get(k) or 0)  # noqa: E731
        usd = (g("input_tokens") * r[0] + g("output_tokens") * r[1] + g("cache_read_tokens") * r[2]
               + g("cache_write_tokens") * r[3]) / 1e6
        return usd, "estimated"
