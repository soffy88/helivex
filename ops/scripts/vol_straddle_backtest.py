#!/usr/bin/env python3
"""Tail-honest short-straddle backtest — does the VRP survive real tails + spread + hedge?

The vol probe showed BTC implied > realized 76% of the time (pseudoSharpe ~1.4).
But selling vol is negative-skew: collect small, lose big in crashes. This models
a SHORT ATM STRADDLE properly and asks: after real tail losses + option spread +
a tail hedge, how much VRP is left?

Data reality (honest): NO historical Deribit options chain in any DB (only DVOL,
the 30d implied-vol index, BTC/ETH 2yr). Deribit API is reachable + NT has a
Deribit adapter (so it's EXECUTABLE in principle), but a real-fill backtest needs
the chain backfilled. So this is a MODEL: Black-Scholes (r=0) option premiums from
DVOL, payoffs from REAL BTC/ETH prices (→ real tail days included), realistic
spread, and a long-OTM-strangle tail hedge. Wings priced at flat IV (no skew) =
hedge is OPTIMISTICALLY CHEAP (crypto put skew makes real wings dearer) — so if
VRP dies even with a cheap hedge, that's decisive.

Reports per asset/tenor: annualised return, Sharpe WITH fat tails, max drawdown,
worst single period, CVaR5%, %positive — naked vs tail-hedged. NOT a gate, no trial.
Run:  ./venv/bin/python ops/scripts/vol_straddle_backtest.py
"""
from __future__ import annotations

import asyncio
import numpy as np
import pandas as pd
import asyncpg
from scipy.stats import norm

HELIOS_DSN  = "postgresql://helios:helios_dev_pass@localhost:5434/helios"
HELIVEX_DSN = "postgresql://helios:helios_dev_pass@localhost:5434/helivex"
ASSETS = {"BTC": "BTC-USDT-SWAP", "ETH": "ETH-USDT-SWAP"}

TENORS = {"weekly": 7, "monthly": 30}
SPREAD_FRAC = 0.05     # option bid-ask as fraction of premium (per leg); sensitivity below
WING = 0.15            # tail hedge: long OTM strangle at ±15%
CVAR_Q = 0.05


def bs(S, K, sigma, T, call):
    if T <= 0 or sigma <= 0:
        return max(0.0, (S - K) if call else (K - S))
    d1 = (np.log(S / K) + 0.5 * sigma**2 * T) / (sigma * np.sqrt(T))
    d2 = d1 - sigma * np.sqrt(T)
    if call:
        return S * norm.cdf(d1) - K * norm.cdf(d2)
    return K * norm.cdf(-d2) - S * norm.cdf(-d1)


async def load_dvol(asset):
    c = await asyncpg.connect(HELIOS_DSN)
    rows = await c.fetch("SELECT ts,value FROM public.raw_features WHERE feature_name='DVOL' AND asset=$1 ORDER BY ts", asset)
    await c.close()
    s = pd.Series([r["value"] for r in rows], index=pd.to_datetime([r["ts"] for r in rows], utc=True).normalize())
    return s[~s.index.duplicated(keep="last")]


async def load_close(inst):
    c = await asyncpg.connect(HELIVEX_DSN)
    rows = await c.fetch("SELECT bar_close_ts,close::float FROM market_data.ohlcv_1h WHERE instrument=$1 AND source='okx_swap' ORDER BY bar_close_ts", inst)
    await c.close()
    s = pd.Series([r[1] for r in rows], index=pd.to_datetime([r[0] for r in rows], utc=True))
    return s.resample("1D").last().dropna()


def stats(r: np.ndarray, ppy: float) -> dict:
    r = r[~np.isnan(r)]
    if len(r) < 5:
        return {}
    eq = np.cumsum(r)
    dd = eq - np.maximum.accumulate(eq)
    cvar = r[r <= np.quantile(r, CVAR_Q)].mean()
    return {
        "n": len(r), "ann_ret": float(r.mean() * ppy),
        "sharpe": float(r.mean() / (r.std() + 1e-12) * np.sqrt(ppy)),
        "maxdd": float(dd.min()), "worst": float(r.min()),
        "cvar5": float(cvar), "pct_pos": float((r > 0).mean()),
        "skew": float(((r - r.mean())**3).mean() / (r.std()**3 + 1e-12)),
    }


def backtest(name, dvol, close):
    idx = dvol.index.intersection(close.index)
    dv = dvol.reindex(idx).sort_index(); px = close.reindex(idx).sort_index()
    print(f"\n{'='*84}\n{name}  ({len(idx)} days {idx.min().date()}→{idx.max().date()})  DVOL avg {dv.mean():.1f}\n{'='*84}")
    dates = list(px.index)
    for tname, T in TENORS.items():
        Ty = T / 365.0
        ppy = 365.0 / T
        naked, hedged, prem_list, eaten = [], [], [], []
        i = 0
        while i + T < len(dates):
            S = px.iloc[i]; sig = dv.iloc[i] / 100.0
            ST = px.iloc[i + T]
            if not (S > 0 and ST > 0 and sig > 0):
                i += T; continue
            # short ATM straddle premium (BS, r=0)
            prem_s = bs(S, S, sig, Ty, True) + bs(S, S, sig, Ty, False)
            move = abs(ST - S)
            # spread: pay half-spread selling
            prem_net = prem_s * (1 - SPREAD_FRAC)
            pnl_naked = (prem_net - move) / S
            # tail hedge: long OTM strangle ±WING (priced flat-IV = cheap)
            Ku, Kd = S * (1 + WING), S * (1 - WING)
            prem_w = bs(S, Ku, sig, Ty, True) + bs(S, Kd, sig, Ty, False)
            wing_payoff = max(ST - Ku, 0) + max(Kd - ST, 0)
            pnl_hedged = (prem_net - prem_w * (1 + SPREAD_FRAC) - move + wing_payoff) / S
            naked.append(pnl_naked); hedged.append(pnl_hedged)
            prem_list.append(prem_s / S); eaten.append(prem_w / S)
            i += T
        sn = stats(np.array(naked), ppy); sh = stats(np.array(hedged), ppy)
        print(f"\n  ── {tname} ({T}d, {sn.get('n',0)} non-overlapping straddles) ──  "
              f"avg ATM premium {np.mean(prem_list)*100:.1f}% spot · wing cost {np.mean(eaten)*100:.2f}% (eats {np.mean(eaten)/np.mean(prem_list)*100:.0f}% of premium)")
        for tag, s in [("NAKED short straddle", sn), (f"HEDGED (+long ±{int(WING*100)}% strangle)", sh)]:
            if not s: continue
            print(f"    {tag:36s} annRet={s['ann_ret']*100:+6.1f}%  Sharpe={s['sharpe']:+5.2f}  "
                  f"maxDD={s['maxdd']*100:6.1f}%  worst1={s['worst']*100:+6.1f}%  CVaR5={s['cvar5']*100:+6.1f}%  "
                  f"pos={s['pct_pos']*100:.0f}%  skew={s['skew']:+.1f}")


async def main():
    print("=" * 84)
    print("Tail-honest short-straddle backtest (BS model · DVOL implied · REAL price tails)")
    print(f"spread={SPREAD_FRAC*100:.0f}%/leg · tail hedge=long ±{int(WING*100)}% strangle (flat-IV=optimistic) · per spot notional")
    print("Data: NO historical Deribit chain in DB (DVOL only); NT has Deribit adapter, API reachable.")
    print("=" * 84)
    for name, inst in ASSETS.items():
        backtest(name, await load_dvol(name), await load_close(inst))
    print(f"\n{'='*84}\nKEY: does VRP survive? NAKED Sharpe with real tails (vs proxy 1.4), and the")
    print("worst1 / CVaR5 ruin numbers. Then HEDGED: does positive expectancy survive the")
    print("hedge cost? (real put skew makes the hedge dearer than modelled here.)")
    print("=" * 84)


if __name__ == "__main__":
    asyncio.run(main())
