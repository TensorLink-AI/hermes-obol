"""Forecasters for hourly spend, best first, chosen by what this machine has.

  ephemeris  Ephemeris API (only when the user has saved a key: that is their opt-in to a network call)
  cascade    Cascade 4M, a 4M-parameter time-series model run locally with onnxruntime on the CPU
  seasonal   pure Python: each future hour's range from the same hour of day over the last 7 days

Every forecaster returns {"tier", "model", "made_at", "start", "quantiles": {"0.1": [...], "0.5": [...],
"0.9": [...]}} for `horizon` hours starting at the hour after the last completed one. The ranges are
marginal per-hour quantiles: nominal, not a measured coverage.
"""
from __future__ import annotations

import hashlib
import json
import math
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

HOUR = 3600
LEVELS = ("0.1", "0.5", "0.9")
MIN_HOURS_MODEL = 24          # below this, models see too little; the seasonal fallback is used


def _quantile(xs, q):
    xs = sorted(xs)
    if not xs:
        return 0.0
    pos = (len(xs) - 1) * q
    lo, hi = math.floor(pos), math.ceil(pos)
    return xs[lo] + (xs[hi] - xs[lo]) * (pos - lo)


# ── seasonal (always available) ──────────────────────────────────────────────

def seasonal(hours, horizon):
    """hours: [(hour_start, usd)] oldest first. Hour-of-day empirical quantiles over the last 7 days.

    With under a day of history there is no daily pattern to read, so every future hour gets the range
    of all hours seen so far.
    """
    values = [v for _, v in hours]
    last = hours[-1][0] if hours else int(time.time() // HOUR) * HOUR - HOUR
    out = {q: [] for q in LEVELS}
    for h in range(1, horizon + 1):
        target = last + h * HOUR
        if len(values) >= 24:
            same = [v for t, v in hours[-24 * 7:] if (target - t) % (24 * HOUR) == 0]
        else:
            same = values
        same = same or [0.0]
        for q in LEVELS:
            out[q].append(max(0.0, _quantile(same, float(q))))
    return {"tier": "seasonal", "model": "hour-of-day quantiles, last 7 days", "made_at": time.time(),
            "start": last + HOUR, "quantiles": out}


# ── cascade (local ONNX) ─────────────────────────────────────────────────────

CASCADE = {
    # Published from the Cascade 4M checkpoint; pinned by revision and sha256 (set at release).
    "repo": "TensorLink-AI/cascade-4m-onnx",
    "revision": "",
    "file": "cascade-4m.onnx",
    "sha256": "",
    "context": 2048,
}


def cascade_path(data_dir: Path) -> Path:
    return Path(data_dir) / "models" / CASCADE["file"]


def cascade_status(data_dir: Path) -> tuple[bool, str]:
    """(usable, why not). Needs numpy + onnxruntime importable and the verified weights on disk."""
    try:
        import numpy  # noqa: F401
        import onnxruntime  # noqa: F401
    except Exception:
        return False, "onnxruntime/numpy not installed on this machine"
    path = cascade_path(data_dir)
    if not path.exists():
        return False, "model not downloaded (/obol model install)"
    if CASCADE["sha256"] and _sha256(path) != CASCADE["sha256"]:
        return False, "model file failed its checksum"
    return True, ""


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def cascade_install(data_dir: Path) -> str:
    """Download the pinned weights from Hugging Face and verify them. Only ever run on the user's command."""
    if not CASCADE["revision"] or not CASCADE["sha256"]:
        return "Cascade 4M weights aren't published yet; Obol is using the seasonal forecaster."
    url = (f"https://huggingface.co/{CASCADE['repo']}/resolve/{CASCADE['revision']}/{CASCADE['file']}")
    path = cascade_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".part")
    with urllib.request.urlopen(url, timeout=120) as r, open(tmp, "wb") as f:
        while chunk := r.read(1 << 20):
            f.write(chunk)
    if _sha256(tmp) != CASCADE["sha256"]:
        tmp.unlink(missing_ok=True)
        return "Download failed its checksum and was deleted."
    tmp.replace(path)
    return f"Cascade 4M installed ({path.stat().st_size // 1024} KB), running locally on the CPU."


_SESSION = {}


def cascade(hours, horizon, data_dir: Path):
    """Export contract: input "values" float32 [1, L] (raw history, the graph scales it itself),
    input "horizon" int64 [1]; output "quantiles" float32 [1, 3, horizon] for levels 0.1, 0.5, 0.9."""
    import numpy as np
    import onnxruntime as ort
    path = str(cascade_path(data_dir))
    if path not in _SESSION:
        opts = ort.SessionOptions()
        opts.intra_op_num_threads = 1      # an edge device is also running the agent
        _SESSION[path] = ort.InferenceSession(path, opts, providers=["CPUExecutionProvider"])
    values = np.asarray([v for _, v in hours][-CASCADE["context"]:], dtype=np.float32)[None, :]
    (q,) = _SESSION[path].run(["quantiles"], {"values": values, "horizon": np.asarray([horizon], dtype=np.int64)})
    q = np.maximum(np.asarray(q)[0], 0.0)
    q = np.sort(q, axis=0)                 # no crossed bands
    return {"tier": "cascade", "model": "Cascade 4M (local, CPU)", "made_at": time.time(),
            "start": hours[-1][0] + HOUR, "quantiles": {lv: q[i].tolist() for i, lv in enumerate(LEVELS)}}


# ── ephemeris (opt-in network) ───────────────────────────────────────────────

EPHEMERIS_API = "https://ephemeris.cascade.industries/api/v1"


def ephemeris(hours, horizon, key: str, model: str = "toto2-313m", timeout: float = 60):
    """One paid call. Sends only the hourly dollar totals: no prompts, models, sessions or names.
    No automatic retries; the Idempotency-Key is chosen before submission."""
    body = {"series": [{"values": [round(v, 6) for _, v in hours][-2048:], "freq": "H"}],
            "horizon": horizon, "quantiles": [float(q) for q in LEVELS]}
    body.update({"mode": "route"} if model in ("", "route") else {"mode": "explicit", "model": model})
    req = urllib.request.Request(f"{EPHEMERIS_API}/forecast", data=json.dumps(body).encode(), method="POST",
                                 headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json",
                                          "Idempotency-Key": f"obol-{uuid.uuid4()}",
                                          "User-Agent": "hermes-obol"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        got = json.load(r)
    qs = got["forecasts"][0]["quantiles"]
    out = {lv: [max(0.0, float(x)) if x is not None else None for x in qs[lv]][:horizon] for lv in LEVELS}
    billing = (got.get("meta") or {}).get("billing") or {}
    return {"tier": "ephemeris", "model": f"Ephemeris {model or 'route'}", "made_at": time.time(),
            "start": hours[-1][0] + HOUR, "quantiles": out,
            "settled_mc": billing.get("settled_mc"), "balance_mc": billing.get("balance_mc")}


# ── choosing ─────────────────────────────────────────────────────────────────

def choose(setting: str, have_key: bool, data_dir: Path, n_hours: int) -> tuple[str, str]:
    """(tier, note). setting: auto | ephemeris | cascade | seasonal."""
    if n_hours < MIN_HOURS_MODEL and setting in ("auto", "ephemeris", "cascade"):
        return "seasonal", f"{n_hours}h of history so far; models start at {MIN_HOURS_MODEL}h"
    if setting in ("auto", "ephemeris") and have_key:
        return "ephemeris", ""
    ok, why = cascade_status(data_dir)
    if setting in ("auto", "cascade", "ephemeris") and ok:
        return "cascade", "" if setting != "ephemeris" else "no Ephemeris key saved"
    if setting == "seasonal":
        return "seasonal", ""
    return "seasonal", why if setting in ("auto", "cascade") else "no Ephemeris key saved"
