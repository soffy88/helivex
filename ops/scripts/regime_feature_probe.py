#!/usr/bin/env python3
"""Path-A feasibility PROBE — do real derivatives features separate BTC regime
better than OHLCV-derived returns+vol?

This is NOT a gate (data too short for a rigorous verdict) and is NOT registered
as a global trial (N stays 10). It answers one yes/no question: is it worth
spending 6-12 months backfilling dominance + ETH/SOL OI to do Path A properly?

Setup (BTC only, daily, ~12.5mo overlap 2025-06 → 2026-06):
  REAL features  = [funding, Δfunding, log(OI), ΔOI%, DVOL]   (helios.raw_features)
  BASE features  = [daily return, 20d realized vol]            (R11's features)
Both: same HMM (GaussianHMM, 3-state, full cov, random_state=42), same short
walk-forward (train 180d / test 30d / monthly retrain), filtered posterior at t-1
(no look-ahead), states labelled bull/neutral/bear by realized TRAIN-window return.

Compare on the OOS test region:
  - per-state OOS annualised mean return / vol / fraction
  - monotonicity (bull > neutral > bear) and return spread  (= clean separation)
  - OOS persistence = does the train-labelled regime still rank returns correctly
    out-of-sample? (R12 failed exactly here)

A-priori features, no tuning to inflate separation.
Run:  ./venv/bin/python ops/scripts/regime_feature_probe.py
"""
from __future__ import annotations

import asyncio
import sys
import warnings
from pathlib import Path

import asyncpg
import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")

HELIOS_DSN  = "postgresql://helios:helios_dev_pass@localhost:5434/helios"
HELIVEX_DSN = "postgresql://helios:helios_dev_pass@localhost:5434/helivex"

N_STATES = 3
SEED     = 42
HMM_ITER = 200
TRAIN    = 180   # days
TEST     = 30
STEP     = 30
PPY      = 365


async def load_helios_daily() -> pd.DataFrame:
    c = await asyncpg.connect(HELIOS_DSN)
    rows = await c.fetch(
        """SELECT ts, feature_name, value FROM public.raw_features
           WHERE asset='BTC' AND feature_name IN ('funding_rate','open_interest_usd','DVOL')
           UNION ALL
           SELECT ts, feature_name, value FROM public.raw_features
           WHERE asset='US-MACRO' AND feature_name IN ('VIXCLS','DFF')""")
    await c.close()
    df = pd.DataFrame([dict(r) for r in rows])
    df["date"] = pd.to_datetime(df["ts"], utc=True).dt.date
    # daily aggregate: funding=mean (intraday avg), others=last value of day
    fund = df[df.feature_name == "funding_rate"].groupby("date")["value"].mean().rename("funding_rate")
    others = (df[df.feature_name != "funding_rate"]
              .pivot_table(index="date", columns="feature_name", values="value", aggfunc="last"))
    piv = others.join(fund, how="outer")
    piv.index = pd.to_datetime(piv.index, utc=True)
    return piv.sort_index()


async def load_btc_price_daily() -> pd.Series:
    c = await asyncpg.connect(HELIVEX_DSN)
    rows = await c.fetch(
        """SELECT bar_close_ts, close::float FROM market_data.ohlcv_1h
           WHERE instrument='BTC-USDT-SWAP' AND source='okx_swap' ORDER BY bar_close_ts""")
    await c.close()
    s = pd.Series([r[1] for r in rows], index=pd.to_datetime([r[0] for r in rows], utc=True))
    return s.resample("1D").last().dropna()


def build_frames():
    helios = asyncio.run(load_helios_daily())
    price = asyncio.run(load_btc_price_daily())
    px = price.reindex(price.index.union(helios.index)).ffill()
    df = helios.copy()
    df["close"] = px.reindex(df.index)
    # macro ffill (sparse), then drop rows w/o funding+OI+close
    for col in ("VIXCLS", "DFF"):
        if col in df: df[col] = df[col].ffill()
    df["ret"] = df["close"].pct_change()
    df["rv20"] = df["ret"].rolling(20).std()
    df["funding"] = df["funding_rate"]
    df["dfunding"] = df["funding_rate"].diff()
    df["log_oi"] = np.log(df["open_interest_usd"])
    df["doi"] = df["open_interest_usd"].pct_change()
    df["dvol"] = df["DVOL"]
    df = df.dropna(subset=["funding", "open_interest_usd", "close", "ret", "rv20", "dvol", "dfunding", "doi"])
    return df


def walk_forward(feat: np.ndarray, ret: np.ndarray, dates):
    """Filtered-posterior walk-forward 3-state HMM. Returns (regime[], used_mask)
    where regime is bull=2/neutral=1/bear=0 by realized train-window return, only
    on test bars (NaN elsewhere). Uses info ≤ t-1 to label trade bar t."""
    from hmmlearn.hmm import GaussianHMM
    n = len(ret)
    regime = np.full(n, np.nan)
    t = TRAIN
    while t < n:
        tr0, tr1 = t - TRAIN, t
        Xtr = feat[tr0:tr1]
        mu, sd = Xtr.mean(0), Xtr.std(0) + 1e-9
        Xtr_n = (Xtr - mu) / sd
        try:
            m = GaussianHMM(n_components=N_STATES, covariance_type="full",
                            n_iter=HMM_ITER, random_state=SEED, tol=1e-4)
            m.fit(Xtr_n)
        except Exception:
            t += STEP; continue
        # label states by realized train return
        tr_states = m.predict(Xtr_n)
        order = sorted(range(N_STATES),
                       key=lambda s: ret[tr0:tr1][tr_states == s].mean() if (tr_states == s).any() else 0)
        rank = {s: r for r, s in enumerate(order)}   # bear=0..bull=2
        # test region: filtered posterior at t-1 → trade bar t
        te1 = min(t + TEST, n)
        for tt in range(t, te1):
            d0 = max(tr0, tt - TRAIN)
            Xd = (feat[d0:tt] - mu) / sd       # info ≤ tt-1
            if len(Xd) < 2: continue
            try:
                post = m.predict_proba(Xd)[-1]
            except Exception:
                continue
            regime[tt] = rank[int(post.argmax())]
        t += STEP
    return regime


def separation(regime: np.ndarray, ret: np.ndarray) -> dict:
    """OOS separation: per-regime realized return/vol over test bars."""
    out = {}
    names = {0: "bear", 1: "neutral", 2: "bull"}
    means = {}
    for k, nm in names.items():
        mask = regime == k
        if mask.sum() < 3:
            out[nm] = {"n": int(mask.sum()), "ret": None, "vol": None, "frac": 0.0}
            continue
        r = ret[mask]
        out[nm] = {"n": int(mask.sum()), "frac": round(float(mask.mean()), 3),
                   "ret": round(float(np.nanmean(r) * PPY), 3),
                   "vol": round(float(np.nanstd(r) * np.sqrt(PPY)), 3)}
        means[nm] = out[nm]["ret"]
    if len(means) == 3:
        out["_monotone"] = means["bull"] > means["neutral"] > means["bear"]
        out["_spread"] = round(means["bull"] - means["bear"], 3)
    else:
        out["_monotone"] = False; out["_spread"] = None
    # rank-vs-forward-return correlation (OOS persistence): does higher regime → higher next-day ret?
    valid = ~np.isnan(regime)
    if valid.sum() > 10:
        rr = ret[valid]; gg = regime[valid]
        if np.std(gg) > 0:
            out["_rank_corr"] = round(float(np.corrcoef(gg, rr)[0, 1]), 3)
        else:
            out["_rank_corr"] = 0.0
    else:
        out["_rank_corr"] = None
    out["_n_test"] = int((~np.isnan(regime)).sum())
    return out


def show(tag: str, sep: dict):
    print(f"\n── {tag} ── (OOS test bars: {sep['_n_test']})")
    for nm in ("bull", "neutral", "bear"):
        s = sep[nm]
        if s["ret"] is None:
            print(f"   {nm:8s} degenerate (n={s['n']})")
        else:
            print(f"   {nm:8s} ret={s['ret']:+.2f}  vol={s['vol']:.2f}  frac={s['frac']:.2f}  n={s['n']}")
    print(f"   monotone(bull>neut>bear): {sep['_monotone']}   ret_spread(bull-bear): {sep['_spread']}   "
          f"rank↔fwd-ret corr: {sep['_rank_corr']}")


def main():
    print("=" * 76)
    print("Path-A feasibility PROBE — BTC real-derivatives vs returns+vol regime")
    print("(NOT a gate, NOT a global trial; data short ~12.5mo, probe only)")
    print("=" * 76)
    df = build_frames()
    print(f"\nAligned BTC daily: {len(df)} days  {df.index[0].date()} → {df.index[-1].date()}")
    print(f"Walk-forward: train={TRAIN}d test={TEST}d step={STEP}d  HMM 3-state full-cov seed={SEED}")

    ret = df["ret"].to_numpy()
    REAL = df[["funding", "dfunding", "log_oi", "doi", "dvol"]].to_numpy()
    BASE = df[["ret", "rv20"]].to_numpy()
    REALM = df[["funding", "dfunding", "log_oi", "doi", "dvol", "VIXCLS", "DFF"]].to_numpy() if "VIXCLS" in df else None

    sep_real = separation(walk_forward(REAL, ret, df.index), ret)
    sep_base = separation(walk_forward(BASE, ret, df.index), ret)
    show("REAL derivatives [funding,Δfunding,logOI,ΔOI,DVOL]", sep_real)
    show("BASE returns+vol [ret, rv20]  (R11 features)", sep_base)
    if REALM is not None:
        show("REAL+macro [+VIX,DFF] (secondary)", separation(walk_forward(REALM, ret, df.index), ret))

    print("\n" + "=" * 76)
    print("PROBE VERDICT")
    print("=" * 76)
    def score(s): return (1 if s["_monotone"] else 0, s["_spread"] or -9, s["_rank_corr"] or -9)
    better = score(sep_real) > score(sep_base)
    print(f"REAL monotone={sep_real['_monotone']} spread={sep_real['_spread']} corr={sep_real['_rank_corr']}")
    print(f"BASE monotone={sep_base['_monotone']} spread={sep_base['_spread']} corr={sep_base['_rank_corr']}")
    print(f"\nReal derivatives features separate BTC regime better than returns+vol: {better}")
    print("Interpretation: monotone + meaningful spread + positive rank↔fwd-ret corr OOS")
    print("= regime is real AND persistent. If REAL clearly beats BASE → Path A worth")
    print("backfilling (dominance + ETH/SOL OI) for the rigorous version. If neither")
    print("separates / no OOS persistence → Path A core assumption not supported.")


if __name__ == "__main__":
    main()
