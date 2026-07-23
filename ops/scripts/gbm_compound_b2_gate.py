#!/usr/bin/env python3
"""Compound-B2 probe — rigorous nonlinear composite alpha via GBM (trial #28).

Follow-up to gbm_compound_gate.py (trial #12, BTC/4h/10-feat/regression → FAIL).
This is the HARD version the Wiki question demands ("single signals fail — does a
nonlinear *composite* work?"):

  - PUBLIC factor library: ~40 pure-TIME-SERIES factors (Alpha101-intraday,
    Jegadeesh momentum, Amihud illiquidity, George-Hwang 52w-high, Qlib158-style
    tech, ts operators). RANK/cross-sectional operators EXCLUDED (meaningless for
    a single instrument) — that is the whole alpha101/gtja191 rank family gone.
  - + helivex signals (trend / mean-reversion-z / vwap-dev / realized-vol)
  - + helios REAL data per asset availability (funding 3-asset; OI BTC; DVOL+VRP
    BTC/ETH). Data reality forces per-asset feature sets (SOL has no DVOL at all).
  - STRICT anti-false-positive discipline:
      · ADF stationarity (p<0.05) on every feature on TRAIN only; non-stationary
        → first-difference, else rolling-z. Report pass rate.
      · target = STATIONARY forward direction (sign of H-bar fwd return), binary.
      · single-factor OOS IC selection → keep top |IC|, cap ≤15, SELECTION ON
        TRAIN ONLY (no test peeking).
      · LightGBM, hyperparams FIXED a-priori (no tuning-to-score), early-stopping
        on an internal val split is the only fit knob.
      · strict expanding walk-forward, monthly retrain, causal features
        (merge_asof backward), no look-ahead.
  - HONEST cost: gross direction accuracy then charged real maker 2bps / taker
    5bps (105-fill measured); turnover reported (this is where short-horizon
    factors die).
  - Full helivex gate: DSR + dual PBO + CPCV walk-forward, GLOBAL trial N (honest
    — .gate_trials.json already has 27, so this is #28, bar rises accordingly).
  - permutation + gain feature importance → does it lean on stable interactions
    or a single noise feature?

Decisive read: OOS IC ≫0 AND stable across halves AND net-of-cost positive AND
passes gate(N=28) → first real helivex composite alpha. OOS IC ~0 / net negative
→ ML just fit noise more elegantly (regime/scalp death), composite doesn't save it.

Run:  ./venv/bin/python ops/scripts/gbm_compound_b2_gate.py [--register]
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import warnings
from pathlib import Path

import asyncpg
import numpy as np
import pandas as pd
from scipy.stats import spearmanr

PROJ = Path(__file__).parent.parent.parent
sys.path.insert(0, str(PROJ))
from tools.strategy_gate import (  # noqa: E402
    _walk_forward_gate,
    _sharpe,
    _dsr_threshold,
    _load_trials,
    _save_trial,
)

warnings.filterwarnings("ignore")

HELIOS = "postgresql://helios:helios_dev_pass@localhost:5434/helios"
HELIVEX = "postgresql://helios:helios_dev_pass@localhost:5434/helivex"

# ── config (a-priori, fixed) ─────────────────────────────────────────────────
START = "2025-05-07"  # funding start (3-asset); OI/DVOL merged where available
END = "2026-07-10"  # clean 1h ohlcv end
SOURCE = "okx_swap_1h"  # TRUE 1h bars (source 'okx_swap' in same table is 4h)
H = 24  # forward target horizon = 24h
PERIODS_1H = 24 * 365  # 8760, for annualised Sharpe
INIT_FRAC = 0.5  # first 50% = initial train
RETRAIN = 720  # walk-forward retrain step ≈ monthly (30d × 24h)
MAX_FEATURES = 15  # cap after IC selection
IC_FLOOR = 0.0  # keep |train IC| strictly > this
MAKER_BPS, TAKER_BPS = 2.0, 5.0
SEED = 42
N_SPLITS, EMBARGO, PBO_THR = 6, 50, 0.5

ASSETS = {
    "BTC-USDT-SWAP": {"asset": "BTC", "funding": True, "oi": True, "dvol": True},
    "ETH-USDT-SWAP": {"asset": "ETH", "funding": True, "oi": False, "dvol": True},
    "SOL-USDT-SWAP": {"asset": "SOL", "funding": True, "oi": False, "dvol": False},
}

# LightGBM hyperparams — FIXED a-priori, never tuned to score.
LGB_PARAMS = dict(
    objective="binary",
    num_leaves=15,
    max_depth=3,
    learning_rate=0.03,
    n_estimators=600,
    min_child_samples=50,
    reg_lambda=1.0,
    subsample=0.8,
    subsample_freq=1,
    colsample_bytree=0.8,
    random_state=SEED,
    n_jobs=2,
    verbose=-1,
)
EPS = 1e-9


# ── DB ───────────────────────────────────────────────────────────────────────
async def _q(dsn, sql, *a):
    c = await asyncpg.connect(dsn)
    try:
        return await c.fetch(sql, *a)
    finally:
        await c.close()


async def _load_asset(instrument: str, cfg: dict) -> pd.DataFrame:
    rows = await _q(
        HELIVEX,
        """SELECT bar_close_ts ts, open::float o, high::float h, low::float l,
                  close::float c, volume::float v
           FROM market_data.ohlcv_1h
           WHERE instrument=$1 AND source=$2 AND bar_close_ts>=$3 AND bar_close_ts<=$4
           ORDER BY bar_close_ts""",
        instrument,
        SOURCE,
        pd.Timestamp(START, tz="UTC"),
        pd.Timestamp(END, tz="UTC"),
    )
    df = pd.DataFrame([dict(r) for r in rows])
    df["ts"] = pd.to_datetime(df["ts"], utc=True)
    df = df.sort_values("ts").reset_index(drop=True)

    async def helios(fn: str) -> pd.DataFrame:
        r = await _q(
            HELIOS,
            "SELECT ts, value FROM public.raw_features WHERE asset=$1 AND feature_name=$2 ORDER BY ts",
            cfg["asset"],
            fn,
        )
        s = pd.DataFrame([dict(x) for x in r])
        if s.empty:
            return s
        s["ts"] = pd.to_datetime(s["ts"], utc=True)
        return s.sort_values("ts")

    def asof(s: pd.DataFrame, col: str):
        if s is None or s.empty:
            return np.nan
        m = pd.merge_asof(
            df[["ts"]], s.rename(columns={"value": col}), on="ts", direction="backward"
        )
        return m[col].to_numpy()

    if cfg["funding"]:
        df["funding_raw"] = asof(await helios("funding_rate"), "funding_raw")
    if cfg["oi"]:
        df["oi_raw"] = asof(await helios("open_interest_usd"), "oi_raw")
    if cfg["dvol"]:
        df["dvol_raw"] = asof(await helios("DVOL"), "dvol_raw")
    return df


# ── factor library (pure time-series only; NO rank / cross-sectional) ─────────
def _decay_linear(x: pd.Series, n: int) -> pd.Series:
    w = np.arange(1, n + 1, dtype=float)
    w /= w.sum()
    return x.rolling(n).apply(lambda a: np.dot(a, w), raw=True)


def build_features(df: pd.DataFrame, cfg: dict) -> tuple[pd.DataFrame, list[str]]:
    o, h, l, c, v = df["o"], df["h"], df["l"], df["c"], df["v"]
    ret = c.pct_change()
    df["ret"] = ret
    F: list[str] = []

    def add(name, series):
        df[name] = series
        F.append(name)

    # momentum (multi-window) — Jegadeesh
    for k in (5, 10, 20, 60, 120):
        add(f"mom_{k}", c / c.shift(k) - 1)
    # intraday / Alpha101#101 & open-close
    add("intra_mom", (c - o) / (h - l + EPS))
    add("co_ret", (c - o) / (o + EPS))
    add("gap", (o - c.shift(1)) / (c.shift(1) + EPS))
    # George-Hwang nearness to recent high / low
    add("gh_high", c / (h.rolling(480).max() + EPS))
    add("gh_low", c / (l.rolling(480).min() + EPS))
    # realized vol multi-window + structure
    for k in (5, 20, 60):
        add(f"rv_{k}", ret.rolling(k).std())
    add("rv_ratio", df["rv_5"] / (df["rv_20"] + EPS))
    add("volofvol", df["rv_20"].rolling(20).std())
    add("parkinson", (np.log(h / l) ** 2).rolling(20).mean() ** 0.5)
    gk = (
        0.5 * np.log(h / l) ** 2
        - (2 * np.log(2) - 1) * np.log((c / o).clip(lower=EPS)) ** 2
    )
    add("gk_vol", gk.rolling(20).mean().clip(lower=0) ** 0.5)
    add("hl_range", ((h - l) / (c + EPS)).rolling(20).mean())
    # volume / liquidity — Amihud
    add("amihud", (ret.abs() / (v + EPS)).rolling(20).mean())
    add("vol_z", (v - v.rolling(20).mean()) / (v.rolling(20).std() + EPS))
    add("dollarvol_chg", (c * v).pct_change().rolling(5).mean())
    add("pv_corr", c.rolling(20).corr(v))
    add("absret_vol_corr", ret.abs().rolling(20).corr(v))
    # technicals / Qlib158-style
    up = ret.clip(lower=0)
    dn = (-ret).clip(lower=0)
    rs = up.rolling(14).mean() / (dn.rolling(14).mean() + EPS)
    add("rsi", 100 - 100 / (1 + rs))
    sma20, std20 = c.rolling(20).mean(), c.rolling(20).std()
    add("mr_z", (c - sma20) / (std20 + EPS))
    add("boll_bw", (2 * std20) / (sma20 + EPS))
    ema12, ema26 = c.ewm(span=12).mean(), c.ewm(span=26).mean()
    macd = (ema12 - ema26) / (c + EPS)
    add("macd", macd)
    add("macd_hist", macd - macd.ewm(span=9).mean())
    add("trend_50", c / c.rolling(50).mean() - 1)
    add("trend_200", c / c.rolling(200).mean() - 1)
    add("decay_mom", _decay_linear(ret, 10))
    add("ts_argmax", c.rolling(20).apply(lambda x: 20 - 1 - np.argmax(x), raw=True))
    add("ts_argmin", c.rolling(20).apply(lambda x: 20 - 1 - np.argmin(x), raw=True))
    add("delta_norm", (c - c.shift(5)) / (std20 + EPS))
    add("skew_20", ret.rolling(20).skew())
    add("kurt_20", ret.rolling(20).kurt())
    add(
        "autocorr1",
        ret.rolling(48).apply(lambda x: pd.Series(x).autocorr(1), raw=False),
    )
    tp = (h + l + c) / 3
    vwap = (tp * v).rolling(20).sum() / (v.rolling(20).sum() + EPS)
    add("vwap_dev", (c - vwap) / (vwap + EPS))

    # ── helios real data (per-asset availability) ──
    if cfg["funding"] and "funding_raw" in df:
        add("funding", df["funding_raw"])
        add("dfunding", df["funding_raw"].diff())
        add("funding_ma", df["funding_raw"].rolling(24).mean())
    if cfg["oi"] and "oi_raw" in df:
        oi = df["oi_raw"].clip(lower=1)
        add("log_oi", np.log(oi))
        add("doi", oi.pct_change())
    if cfg["dvol"] and "dvol_raw" in df:
        add("dvol", df["dvol_raw"])
        rv_ann = (
            ret.rolling(24).std() * np.sqrt(PERIODS_1H) * 100
        )  # annualised %, matches DVOL units
        add("vrp", df["dvol_raw"] - rv_ann)

    return df, F


# ── ADF stationarity (train only) ─────────────────────────────────────────────
def stationarize(
    df: pd.DataFrame, feats: list[str], train_end: int
) -> tuple[list[str], dict]:
    """ADF (p<0.05) each feature on TRAIN slice. Non-stationary → first-diff, re-test;
    still non-stationary → rolling-z (x-mean)/std over 240 bars. Returns final feature
    names + report (raw pass, after-fix pass counts)."""
    from statsmodels.tsa.stattools import adfuller

    def adf_p(x):
        x = x[np.isfinite(x)]
        if len(x) < 60 or np.std(x) < 1e-12:
            return 1.0
        try:
            return adfuller(x, autolag="AIC")[1]
        except Exception:
            return 1.0

    raw_pass = fixed = 0
    for f in list(feats):
        col = df[f].to_numpy(float)
        p = adf_p(col[:train_end])
        if p < 0.05:
            raw_pass += 1
            continue
        # try first difference
        d = df[f].diff()
        if adf_p(d.to_numpy(float)[:train_end]) < 0.05:
            df[f] = d
            fixed += 1
            continue
        # fall back to rolling-z
        z = (df[f] - df[f].rolling(240).mean()) / (df[f].rolling(240).std() + EPS)
        df[f] = z
        if adf_p(z.to_numpy(float)[:train_end]) < 0.05:
            fixed += 1
    return feats, {"n": len(feats), "raw_pass": raw_pass, "fixed": fixed}


# ── single-factor IC selection (train only) ───────────────────────────────────
def select_features(
    df: pd.DataFrame, feats: list[str], fwd: np.ndarray, train_end: int
) -> list[tuple[str, float]]:
    scored = []
    y = fwd[:train_end]
    for f in feats:
        x = df[f].to_numpy(float)[:train_end]
        m = np.isfinite(x) & np.isfinite(y)
        if m.sum() < 100:
            continue
        ic = spearmanr(x[m], y[m]).correlation
        if np.isfinite(ic) and abs(ic) > IC_FLOOR:
            scored.append((f, ic))
    scored.sort(key=lambda t: abs(t[1]), reverse=True)
    return scored[:MAX_FEATURES]


# ── walk-forward GBM ──────────────────────────────────────────────────────────
def walk_forward(X: np.ndarray, ytarget: np.ndarray, init: int) -> np.ndarray:
    import lightgbm as lgb

    n = len(X)
    proba = np.full(n, np.nan)
    t = init
    while t < n:
        # internal val = last 15% of the current train block for early stopping
        vcut = int(t * 0.85)
        m = lgb.LGBMClassifier(**LGB_PARAMS)
        m.fit(
            X[:vcut],
            ytarget[:vcut],
            eval_set=[(X[vcut:t], ytarget[vcut:t])],
            eval_metric="binary_logloss",
            callbacks=[lgb.early_stopping(30, verbose=False), lgb.log_evaluation(0)],
        )
        te = min(t + RETRAIN, n)
        proba[t:te] = m.predict_proba(X[t:te])[:, 1]
        t += RETRAIN
    return proba


def ic_halves(score, fwd):
    m = np.isfinite(score) & np.isfinite(fwd)
    s, f = score[m], fwd[m]
    if len(s) < 30:
        return float("nan"), float("nan"), float("nan"), len(s)
    hh = len(s) // 2
    return (
        spearmanr(s, f).correlation,
        spearmanr(s[:hh], f[:hh]).correlation,
        spearmanr(s[hh:], f[hh:]).correlation,
        len(s),
    )


async def run_asset(instrument: str, cfg: dict, n_trials: int, dsr_thr: float) -> dict:
    df = await _load_asset(instrument, cfg)
    df, feats = build_features(df, cfg)
    c = df["c"].to_numpy(float)
    ret = df["ret"].to_numpy(float)
    fwd = np.concatenate(
        [c[H:] / c[:-H] - 1, np.full(H, np.nan)]
    )  # forward H-bar return
    df["fwd"] = fwd
    df["target"] = (fwd > 0).astype(float)

    # keep rows with all features + target present
    df = df.dropna(subset=feats + ["target", "ret"]).reset_index(drop=True)
    c = df["c"].to_numpy(float)
    ret = df["ret"].to_numpy(float)
    fwd = df["fwd"].to_numpy(float)
    n = len(df)
    init = int(n * INIT_FRAC)

    # ADF on train, then IC-select ≤15 on train
    feats, adf_rep = stationarize(df, feats, init)
    df = df.dropna(subset=feats).reset_index(drop=True)
    c = df["c"].to_numpy(float)
    ret = df["ret"].to_numpy(float)
    fwd = df["fwd"].to_numpy(float)
    n = len(df)
    init = int(n * INIT_FRAC)
    sel = select_features(df, feats, fwd, init)
    sel_names = [s[0] for s in sel]

    X = df[sel_names].to_numpy(float)
    ybin = df["target"].to_numpy(float)
    proba = walk_forward(X, ybin, init)

    # in-sample IC (refit once on train) vs OOS IC halves
    import lightgbm as lgb

    m0 = lgb.LGBMClassifier(**LGB_PARAMS)
    vcut = int(init * 0.85)
    m0.fit(
        X[:vcut],
        ybin[:vcut],
        eval_set=[(X[vcut:init], ybin[vcut:init])],
        eval_metric="binary_logloss",
        callbacks=[lgb.early_stopping(30, verbose=False), lgb.log_evaluation(0)],
    )
    is_ic = spearmanr(m0.predict_proba(X[:init])[:, 1], fwd[:init]).correlation
    oos_ic, oos_h1, oos_h2, noos = ic_halves(proba[init:], fwd[init:])

    # gross direction accuracy (OOS)
    oos_slice = slice(init, n)
    pos = np.zeros(n)
    pos[init:] = np.sign(np.nan_to_num(proba[init:]) - 0.5)
    dir_hit = float(
        np.mean((np.sign(fwd[init:]) == pos[init:])[np.isfinite(fwd[init:])])
    )

    # pnl: position held from t to t+1 earns ret[t+1]; charge turnover cost
    pnl = np.zeros(n)
    pnl[init + 1 :] = pos[init:-1] * ret[init + 1 :]
    turn = np.abs(np.diff(pos[init:], prepend=0.0))
    trades_per_day = float(
        np.mean(turn) * 24
    )  # avg |Δpos| per bar × 24 = daily turnover
    gross_pnl = pnl[init:].copy()
    net_taker = gross_pnl - turn * (TAKER_BPS / 1e4)
    net_maker = gross_pnl - turn * (MAKER_BPS / 1e4)

    # feature importance (gain) + permutation on OOS
    from sklearn.inspection import permutation_importance

    split = int(n * 0.7)
    mP = lgb.LGBMClassifier(**LGB_PARAMS)
    vc = int(split * 0.85)
    mP.fit(
        X[:vc],
        ybin[:vc],
        eval_set=[(X[vc:split], ybin[vc:split])],
        eval_metric="binary_logloss",
        callbacks=[lgb.early_stopping(30, verbose=False), lgb.log_evaluation(0)],
    )
    gain = dict(zip(sel_names, mP.booster_.feature_importance(importance_type="gain")))
    pi = permutation_importance(
        mP, X[split:], ybin[split:], n_repeats=8, random_state=SEED, scoring="roc_auc"
    )
    perm = dict(zip(sel_names, pi.importances_mean))

    # gate on NET (taker) OOS pnl — honest
    gate = _walk_forward_gate(
        net_taker, N_SPLITS, EMBARGO, PERIODS_1H, PBO_THR, purge_bars=H
    )
    from tools.strategy_gate import _deflated_sharpe_real

    dsr_real = _deflated_sharpe_real(
        net_taker, gate["oos_sharpes"], n_trials, PERIODS_1H
    )
    adj = gate["deflated_sharpe"] - dsr_thr
    status = "PASS" if (not gate["fail_reasons"] and adj > 0) else "FAIL"

    return {
        "instrument": instrument,
        "n": n,
        "init": init,
        "n_feats_lib": adf_rep["n"],
        "adf": adf_rep,
        "sel": sel,
        "is_ic": is_ic,
        "oos_ic": oos_ic,
        "oos_h1": oos_h1,
        "oos_h2": oos_h2,
        "noos": noos,
        "dir_hit": dir_hit,
        "trades_per_day": trades_per_day,
        "gross_sharpe": _sharpe(gross_pnl, PERIODS_1H),
        "net_taker_sharpe": _sharpe(net_taker, PERIODS_1H),
        "net_maker_sharpe": _sharpe(net_maker, PERIODS_1H),
        "gate": gate,
        "dsr_real": dsr_real,
        "adj": adj,
        "status": status,
        "gain": gain,
        "perm": perm,
        "date0": str(df["ts"].iloc[0].date()),
        "date1": str(df["ts"].iloc[-1].date()),
    }


async def main(register: bool):
    n_trials = _load_trials()["total_trials"] + 1
    dsr_thr = _dsr_threshold(n_trials)
    print("=" * 90)
    print(
        "Compound-B2 — rigorous nonlinear composite alpha (public factor lib + helios, GBM)"
    )
    print(
        f"public TS factors + helios(per-asset) · ADF · IC-select ≤{MAX_FEATURES} · fixed LGBM · "
        f"walk-forward · honest cost"
    )
    print(
        f"GLOBAL trial #{n_trials}  (N={n_trials}, DSR bar={dsr_thr:.3f})  1h bars, H={H}"
    )
    print("=" * 90)

    results = {}
    for inst, cfg in ASSETS.items():
        r = await run_asset(inst, cfg, n_trials, dsr_thr)
        results[inst] = r
        g = r["gate"]
        print(
            f"\n── {inst}  ({r['date0']}→{r['date1']}, {r['n']} bars, init {r['init']}) ──"
        )
        print(
            f"  factor lib (post-rank-exclude): {r['n_feats_lib']}   "
            f"ADF: raw-stationary {r['adf']['raw_pass']}, fixed {r['adf']['fixed']}"
        )
        print(f"  IC-selected ≤{MAX_FEATURES} (train |IC|):")
        for f, ic in r["sel"]:
            print(
                f"     {f:14s} IC={ic:+.3f}  gain={r['gain'].get(f, 0):8.1f}  perm={r['perm'].get(f, 0):+.4f}"
            )
        print(
            f"  IN-SAMPLE IC {r['is_ic']:+.3f}   OOS IC {r['oos_ic']:+.3f}  "
            f"halves {r['oos_h1']:+.3f}/{r['oos_h2']:+.3f}  nOOS={r['noos']}"
        )
        print(
            f"  gross dir-hit {r['dir_hit'] * 100:.1f}%   turnover {r['trades_per_day']:.2f}/day"
        )
        print(
            f"  Sharpe: gross {r['gross_sharpe']:+.2f} | net-maker(2bp) {r['net_maker_sharpe']:+.2f} "
            f"| net-taker(5bp) {r['net_taker_sharpe']:+.2f}"
        )
        print(
            f"  gate(net-taker): meanOOS {g['mean_oos_sharpe']:+.3f}  DSR {g['deflated_sharpe']:+.3f}  "
            f"adjDSR {r['adj']:+.3f}  PBO {g['pbo']:.2f}  CSCV-PBO {g['pbo_cscv']:.2f}  DSRreal {r['dsr_real']:.3f}"
        )
        print(f"  fail: {g['fail_reasons'] or 'none'}   →  {r['status']}")

    overall = "PASS" if any(r["status"] == "PASS" for r in results.values()) else "FAIL"
    print("\n" + "=" * 90)
    print(f"COMPOUND-B2 VERDICT (N={n_trials}): {overall}")
    print("Read: OOS IC≫0 & stable-halves & net>0 & gate PASS → real composite alpha.")
    print(
        "      OOS IC~0 / net<0 → ML fit noise more elegantly; composite doesn't save it."
    )
    print("=" * 90)

    if register:
        metrics = {
            "instruments": {},
            "overall": overall,
            "note": f"Compound-B2 GBM, public TS factor lib + helios per-asset, "
            f"1h H={H}, IC-select≤{MAX_FEATURES}, honest 5bps",
        }
        for inst, r in results.items():
            metrics["instruments"][inst] = {
                "status": r["status"],
                "dsr": r["gate"]["deflated_sharpe"],
                "pbo": r["gate"]["pbo"],
                "mean_oos": r["gate"]["mean_oos_sharpe"],
                "gross_sharpe": r["gross_sharpe"],
                "net_taker_sharpe": r["net_taker_sharpe"],
                "oos_ic": r["oos_ic"],
                "dir_hit": r["dir_hit"],
                "turnover_day": r["trades_per_day"],
            }
        tn = _save_trial(
            "ops/scripts/gbm_compound_b2_gate.py (Compound-B2: factor-lib+helios GBM)",
            overall,
            metrics,
        )
        print(f"\nRegistered as global trial #{tn}")
    else:
        print("\n(dry run — not registered; pass --register)")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--register", action="store_true")
    asyncio.run(main(ap.parse_args().register))
