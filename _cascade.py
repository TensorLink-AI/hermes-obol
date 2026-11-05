"""Cascade 4M (toto2-4m, bo_best) in pure numpy: no torch, no onnxruntime, runs on any CPU.

A line-for-line port of the checkpoint's own model.py + forecast_wrapper.py decode, checked against the
torch reference (tests/test_cascade_parity.py). Built at the trained context (4096 = 128 patches), since the
u-muP residual coefficients depend on it. Histories are not left-padded to the full window: each is
rounded up to the next power-of-two number of patches (4..128), the length ladder the model saw in training.
"""
from __future__ import annotations

import json
import math
import struct
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
WEIGHTS = HERE / "models" / "cascade-4m.safetensors"
CONFIG = HERE / "models" / "cascade-4m.json"
LADDER = (4, 8, 16, 32, 64, 128)          # patches
STABLE_DECODE_STEPS = 768
Z_CLAMP = 64.0


def load_safetensors(path: Path) -> dict:
    raw = Path(path).read_bytes()
    (n,) = struct.unpack("<Q", raw[:8])
    header = json.loads(raw[8:8 + n])
    base = 8 + n
    out = {}
    for name, meta in header.items():
        if name == "__metadata__":
            continue
        if meta["dtype"] != "F32":
            raise ValueError(f"{name}: unsupported dtype {meta['dtype']}")
        s, e = meta["data_offsets"]
        out[name] = np.frombuffer(raw, dtype="<f4", count=(e - s) // 4, offset=base + s).reshape(meta["shape"])
    return out


def _erf(x):
    # exact erf, as torch.nn.GELU() uses
    return np.vectorize(math.erf, otypes=[np.float64])(x)


def _gelu(x):
    return (0.5 * x * (1.0 + _erf(x.astype(np.float64) / math.sqrt(2.0)))).astype(np.float32)


def _silu(x):
    return x / (1.0 + np.exp(-x))


def _ln(x, eps=1e-4):
    m = x.mean(-1, keepdims=True)
    v = ((x - m) ** 2).mean(-1, keepdims=True)
    return (x - m) / np.sqrt(v + eps)


def _softplus(x):
    return np.log1p(np.exp(-np.abs(x))) + np.maximum(x, 0)


class Cascade4M:
    def __init__(self, weights: Path = WEIGHTS, config: Path = CONFIG):
        cfg = json.loads(Path(config).read_text())
        c = cfg["toto2"]
        self.levels = [float(q) for q in cfg["quantile_levels"]]
        self.d, self.L, self.H, self.hd = c["d_model"], c["num_layers"], c["num_heads"], c["head_dim"]
        self.ps, self.nq, self.ctx = c["patch_size"], c["num_quantiles"], c["context_length"]
        self.max_patches, self.group = c["max_patches"], c["layer_group_size"]
        self.w = load_safetensors(weights)
        S = max(2.0, self.ctx / self.ps)
        ratio2 = S / math.log(S)
        af2 = 2.0 * c["residual_mult"] ** 2 / (ratio2 + 1.0)
        aa2 = ratio2 * af2
        Lr = 2.0 * self.L
        self.coef = []
        for i in range(self.L):
            ta = aa2 / (Lr / 2.0 + i * aa2 + i * af2)
            tm = af2 / (Lr / 2.0 + (i + 1) * aa2 + i * af2)
            self.coef.append((math.sqrt(ta / (ta + 1)), math.sqrt(1 / (ta + 1)),
                              math.sqrt(tm / (tm + 1)), math.sqrt(1 / (tm + 1))))
        half = self.hd // 2
        idx = np.arange(half, dtype=np.float32) / max(1, half)
        self.inv_freq = (1.0 / (10000.0 ** idx)).astype(np.float32)
        self.zeta = ((idx + 0.4) / 1.4).astype(np.float32)

    # ── network ──────────────────────────────────────────────────────────────
    def _lin(self, x, name, bias=True):
        y = x @ self.w[name + ".weight"].T
        return y + self.w[name + ".bias"] if bias else y

    def _rmlp(self, x, name):
        return x + self._lin(_silu(self._lin(x, name + ".net.0", False)), name + ".net.2", False)

    def _xpos(self, q, k):
        T = q.shape[-2]
        t = np.arange(T, dtype=np.float32)
        freqs = np.outer(t, self.inv_freq)
        cos = np.repeat(np.cos(freqs), 2, axis=-1)
        sin = np.repeat(np.sin(freqs), 2, axis=-1)
        power = ((t - T // 2) / 512.0)[:, None]
        scale = np.repeat(self.zeta[None, :] ** power, 2, axis=-1)

        def rot(x):
            x1, x2 = x[..., 0::2], x[..., 1::2]
            return np.stack((-x2, x1), axis=-1).reshape(x.shape)
        return (q * cos + rot(q) * sin) * scale, (k * cos + rot(k) * sin) / scale

    def _block(self, x, i):
        a_a, a_b, m_a, m_b = self.coef[i]
        p = f"blocks.{i}"
        T = x.shape[0]
        qkv = self._lin(_ln(x), p + ".qkv").reshape(T, 3, self.H, self.hd)
        q, k, v = (qkv[:, j].transpose(1, 0, 2) for j in range(3))      # (H, T, hd)
        if i % self.group != self.group - 1:                            # time layer: causal, xPos
            q, k = self._xpos(q, k)
            q = q * (_softplus(self.w[p + ".per_dim_scale"]) / math.log(2.0))
            s = (q @ k.transpose(0, 2, 1)) / self.hd
            s = np.where(np.tril(np.ones((T, T), dtype=bool)), s, -np.inf)
            s = np.exp(s - s.max(-1, keepdims=True))
            attn = (s / s.sum(-1, keepdims=True)) @ v
        else:                                                           # variate layer at C = 1: softmax over one
            attn = v
        attn = attn.transpose(1, 0, 2).reshape(T, self.H * self.hd)
        x = a_b * x + a_a * self._lin(attn, p + ".proj")
        h = _gelu(self._lin(_ln(x), p + ".mlp.0", False))
        return m_b * x + m_a * self._lin(h, p + ".mlp.2", False)

    def _forward(self, patches, mask):
        """patches, mask: (P, ps) float32 -> (P, ps, nq)."""
        x = np.concatenate([patches * (1.0 - mask), mask], axis=-1)
        x = self._rmlp(self._lin(x, "patch_embed"), "embed_mlp")
        for i in range(self.L):
            x = self._block(x, i)
        x = self._rmlp(_ln(x), "out_mlp")
        return self._lin(x, "head").reshape(x.shape[0], self.ps, self.nq)

    # ── decoding ─────────────────────────────────────────────────────────────
    @staticmethod
    def _standardize(x, min_obs=8, eps=1e-5):
        x = x.astype(np.float64)
        ref = x[0]
        xk = x - ref
        n = np.arange(1, x.shape[0] + 1, dtype=np.float64)
        loc = np.cumsum(xk) / n
        var = np.cumsum(xk * xk) / n - loc * loc
        loc = loc + ref
        scale = np.maximum(np.sqrt(np.maximum(var, 0.0)), eps)
        if x.shape[0] >= min_obs:
            loc[:min_obs - 1], scale[:min_obs - 1] = loc[min_obs - 1], scale[min_obs - 1]
        else:
            loc[:], scale[:] = loc[-1], scale[-1]
        z = np.clip(np.arcsinh((x - loc) / scale), -Z_CLAMP, Z_CLAMP)
        return z, loc, scale

    def prep(self, history):
        h = np.asarray(history, dtype=np.float64).reshape(-1)
        n_ctx = self.ctx // self.ps
        rungs = [r for r in LADDER if r <= n_ctx] or [n_ctx]
        want = next((r for r in rungs if r * self.ps >= h.shape[0]), rungs[-1]) * self.ps
        if h.shape[0] >= want:
            return h[-want:]
        return np.concatenate([np.full(want - h.shape[0], h[0] if h.size else 0.0), h])

    def _decode_block(self, z, block):
        ps = self.ps
        ctx_p = min(z.shape[0] // ps, self.max_patches - block)
        ctx = z[-ctx_p * ps:].reshape(ctx_p, ps).astype(np.float32)
        patches = np.concatenate([ctx, np.zeros((block, ps), np.float32)])
        mask = np.zeros_like(patches)
        mask[ctx_p:] = 1.0
        pred = self._forward(patches, mask)[ctx_p - 1: ctx_p + block - 1]
        return np.sort(pred, axis=-1).reshape(block * ps, self.nq)

    def quantiles(self, history, horizon: int) -> np.ndarray:
        """(horizon, 9) real-space quantiles at levels 0.1..0.9."""
        x = self.prep(history)
        ps = self.ps
        stable = max(1, min(STABLE_DECODE_STEPS // ps, self.max_patches - 2))
        remaining = -(-int(horizon) // ps)
        lo = hi = None
        out = []
        while remaining > 0:
            block = min(remaining, stable)
            z, loc, scale = self._standardize(x)
            if lo is None:
                lo, hi = x.min() - 1e4 * scale[-1], x.max() + 1e4 * scale[-1]
            q = np.sinh(self._decode_block(z, block).astype(np.float64)) * scale[-1] + loc[-1]
            q = np.clip(q, lo, hi)
            out.append(q)
            remaining -= block
            if remaining > 0:
                x = np.concatenate([x, q[:, self.nq // 2]])
        return np.concatenate(out)[: int(horizon)]


_MODEL = None


def model() -> Cascade4M:
    global _MODEL
    if _MODEL is None:
        _MODEL = Cascade4M()
    return _MODEL
