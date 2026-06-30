#!/usr/bin/env python3
"""Volatility-trading feasibility PROBE (new direction — helivex has only ever
done directional). Zero-cost: helios DVOL (Deribit implied vol, BTC/ETH 2yr daily)
+ helivex OHLCV (realized vol). NOT a gate, NOT a trial (N stays 11).

Question: is there a DIFFERENT alpha source here than directional/regime? The
regime probes all showed OOS corr ~0 — does VRP / IV-timing show real OOS signal?

Probe 1 — Volatility Risk Premium (VRP = implied − realized):
  - Does the premium exist (implied > realized on average)?
  - Does VRP_t predict the short-vol payoff (collect implied, pay future realized)?
  - Rank IC (Spearman), split-half to check OOS persistence.
Probe 2 — IV-level timing:
  - Does DVOL z-score predict forward returns (high IV → bounce? low IV → drift)?

HONEST: perp+OHLCV approximation of vol trading, NOT real options vol-arb (that
needs the Deribit options chain). short_vol_payoff = DVOL_t − realized_vol[t+1..t+h]
is a proxy for a delta-hedged short-straddle P&L, not an executable backtest.
Overlapping forward windows inflate significance — treat IC magnitudes, not p-values.

Run:  ./venv/bin/python ops/scripts/vol_trading_probe.py
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import asyncpg
import numpy as np
import pandas as pd
from scipy.stats import spearmanr

HELIOS_DSN  = "postgresql://helios:helios_dev_pass@localhost:5434/helios"
HELIVEX_DSN = "postgresql://helios:helios_dev_pass@localhost:5434/helivex"
ASSETS = {"BTC": "BTC-USDT-SWAP", "ETH": "ETH-USDT-SWAP"}
RV_WIN = 20          # realized-vol lookback (days)
HORIZONS = [5, 10, 20]
IVZ_WIN = 60         # DVOL z-score window


async def load_dvol(asset: str) -> pd.Series:
    c = await asyncpg.connect(HELIOS_DSN)
    rows = await c.fetch("SELECT ts, value FROM public.raw_features WHERE feature_name='DVOL' AND asset=$1 ORDER BY ts", asset)
    await c.close()
    s = pd.Series([r["value"] for r in rows], index=pd.to_datetime([r["ts"] for r in rows], utc=True))
    s.index = s.index.normalize()
    return s[~s.index.duplicated(keep="last")]


async def load_close(inst: str) -> pd.Series:
    c = await asyncpg.connect(HELIVEX_DSN)
    rows = await c.fetch("SELECT bar_close_ts, close::float FROM market_data.ohlcv_1h WHERE instrument=$1 AND source='okx_swap' ORDER BY bar_close_ts", inst)
    await c.close()
    s = pd.Series([r[1] for r in rows], index=pd.to_datetime([r[0] for r in rows], utc=True))
    return s.resample("1D").last().dropna()


def ic_split(x: np.ndarray, y: np.ndarray) -> tuple[float, float, float, int]:
    """Spearman IC: full, first-half, second-half (OOS persistence)."""
    m = ~(np.isnan(x) | np.isnan(y))
    x, y = x[m], y[m]
    n = len(x)
    if n < 30:
        return float("nan"), float("nan"), float("nan"), n
    full = spearmanr(x, y).correlation
    h = n // 2
    ic1 = spearmanr(x[:h], y[:h]).correlation
    ic2 = spearmanr(x[h:], y[h:]).correlation
    return full, ic1, ic2, n


def run_asset(name: str, inst: str):
    dvol = asyncio.run(load_dvol(name))
    close = asyncio.run(load_close(inst))
    idx = dvol.index.intersection(close.index)
    df = pd.DataFrame({"dvol": dvol.reindex(idx), "close": close.reindex(idx)}).sort_index()
    ret = np.log(df["close"]).diff()
    df["rv20"] = ret.rolling(RV_WIN).std() * np.sqrt(365) * 100   # annualized %
    df["vrp"] = df["dvol"] - df["rv20"]
    df["ivz"] = (df["dvol"] - df["dvol"].rolling(IVZ_WIN).mean()) / df["dvol"].rolling(IVZ_WIN).std()

    print(f"\n{'='*78}\n{name}  ({len(df)} days {df.index[0].date()}→{df.index[-1].date()})  "
          f"DVOL avg {df['dvol'].mean():.1f}  RV20 avg {df['rv20'].mean():.1f}  VRP avg {df['vrp'].mean():+.1f}\n{'='*78}")

    # VRP premium existence (short-vol payoff over each horizon, proxy)
    print("Probe 1 — VRP (implied−realized) predictive power")
    print(f"  {'h':>3} {'mean_payoff':>12} {'%pos':>6} {'pseudoSharpe':>13} | {'IC(VRP,payoff)':>15} {'IC1/IC2(OOS)':>16} | {'IC(VRP,fwdret)':>14}")
    for h in HORIZONS:
        fwd_rv = ret.shift(-1).rolling(h).std().shift(-(h - 1)) * np.sqrt(365) * 100  # realized vol over [t+1..t+h]
        payoff = df["dvol"] - fwd_rv                       # collect implied, pay future realized
        fwd_ret = df["close"].shift(-h) / df["close"] - 1
        p = payoff.to_numpy()
        pm = p[~np.isnan(p)]
        psharpe = (np.mean(pm) / (np.std(pm) + 1e-9)) * np.sqrt(365 / h) if len(pm) else float("nan")
        icf, ic1, ic2, _ = ic_split(df["vrp"].to_numpy(), payoff.to_numpy())
        icr, _, _, _ = ic_split(df["vrp"].to_numpy(), fwd_ret.to_numpy())
        print(f"  {h:>3} {np.nanmean(p):>12.2f} {100*np.mean(pm>0):>5.0f}% {psharpe:>13.2f} | "
              f"{icf:>+15.3f} {ic1:>+7.3f}/{ic2:>+7.3f} | {icr:>+14.3f}")

    # IV-level timing
    print("Probe 2 — DVOL z-score timing → forward return")
    print(f"  {'h':>3} {'IC(IVz,fwdret)':>15} {'IC1/IC2(OOS)':>16}")
    for h in HORIZONS:
        fwd_ret = df["close"].shift(-h) / df["close"] - 1
        icf, ic1, ic2, _ = ic_split(df["ivz"].to_numpy(), fwd_ret.to_numpy())
        print(f"  {h:>3} {icf:>+15.3f} {ic1:>+7.3f}/{ic2:>+7.3f}")
    return df


def main():
    print("=" * 78)
    print("Volatility-trading feasibility PROBE — VRP + IV timing (BTC/ETH, 2yr daily)")
    print("NOT a gate, NOT a trial (N stays 11). Perp+OHLCV proxy, not real options arb.")
    print("=" * 78)
    for name, inst in ASSETS.items():
        run_asset(name, inst)
    print(f"\n{'='*78}\nREADING THE PROBE")
    print("- VRP premium real & harvestable: mean_payoff > 0, %pos high, pseudoSharpe meaningful.")
    print("- VRP timing adds: IC(VRP,payoff) > 0 AND stable across IC1/IC2 (OOS persistent).")
    print("- IV timing: IC(IVz,fwdret) ≠ 0 and stable.")
    print("- vs regime probes (OOS corr ~0): if these ICs are materially non-zero AND")
    print("  stable across halves → DIFFERENT, real alpha source → worth costing out options.")
    print("  If ICs ~0 / flip sign across halves → no edge in available data either.")
    print("=" * 78)


if __name__ == "__main__":
    main()
