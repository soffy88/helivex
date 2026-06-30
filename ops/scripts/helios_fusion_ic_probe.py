#!/usr/bin/env python3
"""PROBE — do helios's 13 fused dimensions predict forward returns OOS? (NOT a gate.)

helios persists, going forward, two new things helivex never gated on:
  - ts_fusion_dimensions : 13 fused signal dims per (time, symbol) — trend, flow,
    volume, support_resistance_distance, decision_trail_history, sector_rotation,
    engine_consensus, macro, sentiment, seasonality, derivatives, regime, onchain
  - ts_forward_returns   : realized forward return at horizon_min (60/240), labelled
    at decision time — a ready-made supervised label.

Decisive question this probe answers cheaply: does ANY fused dimension carry
out-of-sample-stable rank correlation with the forward return — i.e. is there a
signal worth building a durable capture pipeline for? Some dims are derived from
data helivex already gated to FAIL (derivatives/macro/regime ← funding/OI/DVOL/FRED),
so those are expected dead; the interesting ones are the live/fused ones (flow,
onchain, engine_consensus, trend, sentiment).

HONEST LIMITS (this is a PROBE, gate ledger N stays 13):
  - Only ~3-5 days of overlapping data exist (fusion since 2026-06-23, fwd_returns
    since 2026-06-24). 60-min forward windows sampled every ~7.5s overlap massively →
    p-values are meaningless. Read IC MAGNITUDE and SIGN-PERSISTENCE across halves,
    never significance.
  - To blunt the overlap we also compute IC on a 60-min NON-OVERLAPPING subsample
    (~tens of obs/symbol — tiny, directional only).
  - A real verdict needs months of history; this only decides whether to invest in
    capturing it. Mirrors ops/scripts/vol_trading_probe.py.

Run:  ./venv/bin/python ops/scripts/helios_fusion_ic_probe.py
"""
from __future__ import annotations

import asyncio
import sys

import asyncpg
import numpy as np
import pandas as pd
from scipy.stats import spearmanr

HELIOS = "postgresql://helios:helios_dev_pass@localhost:5434/helios"
SYMBOLS = ["BTC-USDT", "ETH-USDT", "SOL-USDT"]
HORIZON_MIN = 60
DIMS = ["trend", "flow", "volume", "support_resistance_distance",
        "decision_trail_history", "sector_rotation", "engine_consensus",
        "macro", "sentiment", "seasonality", "derivatives", "regime", "onchain"]


async def _load(symbol: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    c = await asyncpg.connect(HELIOS)
    try:
        frows = await c.fetch(
            "SELECT time, dim_name, value FROM ts_fusion_dimensions "
            "WHERE symbol=$1 ORDER BY time", symbol)
        rrows = await c.fetch(
            "SELECT time, fwd_return FROM ts_forward_returns "
            "WHERE symbol=$1 AND horizon_min=$2 ORDER BY time", symbol, HORIZON_MIN)
    finally:
        await c.close()
    fdf = pd.DataFrame([dict(r) for r in frows])
    rdf = pd.DataFrame([dict(r) for r in rrows])
    return fdf, rdf


def _wide(fdf: pd.DataFrame) -> pd.DataFrame:
    """Long → wide: index=time, one column per dim_name (value)."""
    w = fdf.pivot_table(index="time", columns="dim_name", values="value", aggfunc="last")
    w.index = pd.to_datetime(w.index, utc=True)
    return w.sort_index()


def _merge(wide: pd.DataFrame, rdf: pd.DataFrame, subsample_60m: bool) -> pd.DataFrame:
    r = rdf.copy()
    r["time"] = pd.to_datetime(r["time"], utc=True)
    r = r.sort_values("time").dropna(subset=["fwd_return"])
    if subsample_60m:
        # one forward-return row per 60-min bin → ~non-overlapping windows
        r = r.set_index("time").resample("60min")["fwd_return"].last().dropna().reset_index()
    # attach the most recent fusion vector at/just before each label time
    merged = pd.merge_asof(
        r, wide.reset_index().rename(columns={"index": "time"}),
        on="time", direction="backward", tolerance=pd.Timedelta("180s"))
    return merged.dropna(subset=["fwd_return"])


def _ic_halves(x: np.ndarray, y: np.ndarray) -> tuple[float, float, float, int]:
    m = ~(np.isnan(x) | np.isnan(y))
    x, y = x[m], y[m]
    n = len(x)
    if n < 12 or np.nanstd(x) < 1e-12:
        return np.nan, np.nan, np.nan, n
    full = spearmanr(x, y).correlation
    h = n // 2
    ic1 = spearmanr(x[:h], y[:h]).correlation if h >= 6 else np.nan
    ic2 = spearmanr(x[h:], y[h:]).correlation if (n - h) >= 6 else np.nan
    return full, ic1, ic2, n


def _report(title: str, frames: dict[str, pd.DataFrame]) -> list[dict]:
    print(f"\n{'='*94}\n{title}\n{'='*94}")
    print(f"{'dimension':30s} " + "  ".join(f"{s.split('-')[0]:>14s}" for s in SYMBOLS) + "   pooled|h1|h2")
    out = []
    for dim in DIMS:
        cells = []
        pooled_x, pooled_y = [], []
        stable_signs = []
        for s in SYMBOLS:
            df = frames[s]
            if dim not in df.columns:
                cells.append(f"{'--':>14s}"); continue
            x = df[dim].to_numpy(float); y = df["fwd_return"].to_numpy(float)
            full, ic1, ic2, n = _ic_halves(x, y)
            pooled_x.append(x); pooled_y.append(y)
            if not np.isnan(ic1) and not np.isnan(ic2):
                stable_signs.append(np.sign(ic1) == np.sign(ic2) and abs(full) > 0.03)
            cells.append(f"{full:+.2f}({n:>4})" if not np.isnan(full) else f"{'--':>14s}")
        # pooled (z-score each symbol's feature first so ranks are comparable)
        px = np.concatenate([(_z(a)) for a in pooled_x]) if pooled_x else np.array([])
        py = np.concatenate(pooled_y) if pooled_y else np.array([])
        pf, ph1, ph2, pn = _ic_halves(px, py)
        flag = ""
        if not np.isnan(pf) and abs(pf) > 0.03 and not np.isnan(ph1) and not np.isnan(ph2) \
                and np.sign(ph1) == np.sign(ph2):
            flag = "  <<< OOS-stable sign + |IC|>0.03"
        print(f"{dim:30s} " + "  ".join(cells) + f"   {pf:+.3f}|{ph1:+.2f}|{ph2:+.2f}{flag}")
        out.append({"dim": dim, "pooled_ic": pf, "h1": ph1, "h2": ph2, "n": pn, "stable": bool(flag)})
    return out


def _z(a: np.ndarray) -> np.ndarray:
    a = a.astype(float); s = np.nanstd(a)
    return (a - np.nanmean(a)) / s if s > 1e-12 else a * 0.0


async def main() -> None:
    print("="*94)
    print(f"helios fused-dim → forward-return ({HORIZON_MIN}min) IC PROBE — NOT a gate (N stays 13)")
    print("IC magnitude + sign-persistence across halves only; p-values meaningless (overlap).")
    raw = {s: await _load(s) for s in SYMBOLS}
    spans = {s: (len(raw[s][0]), len(raw[s][1])) for s in raw}
    print("rows (fusion_long, fwd_returns):", spans)

    overlap = {s: _merge(_wide(raw[s][0]), raw[s][1], subsample_60m=False) for s in SYMBOLS}
    sub     = {s: _merge(_wide(raw[s][0]), raw[s][1], subsample_60m=True)  for s in SYMBOLS}
    print("merged obs/symbol — overlapping:", {s: len(overlap[s]) for s in SYMBOLS},
          "| 60m-subsample:", {s: len(sub[s]) for s in SYMBOLS})

    r1 = _report("A) OVERLAPPING (autocorr-inflated — magnitudes only)", overlap)
    r2 = _report("B) 60-MIN NON-OVERLAPPING SUBSAMPLE (tiny n, the honest read)", sub)

    print(f"\n{'='*94}\nVERDICT")
    stable_b = [r["dim"] for r in r2 if r["stable"]]
    if stable_b:
        print(f"  Candidate dims with OOS-stable sign + |IC|>0.03 in the subsample: {stable_b}")
        print("  → worth building a durable capture pipeline + re-probing with months of history.")
    else:
        print("  NO fused dimension shows OOS-stable sign with |IC|>0.03 in the non-overlapping")
        print("  subsample. On 3-5 days this is inconclusive, NOT a kill — but it means there is")
        print("  no early signal strong enough to justify capture infra yet. Re-run as history grows.")
    print("  Reminder: ~3-5 day window, no real OOS. Decides capture-investment, not deployment.")
    print("="*94)


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
