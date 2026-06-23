#!/usr/bin/env python3
"""R12 — Cross-Asset Single Regime (final HMM attempt).

Hypothesis (not blind — built on R11's evidence)
------------------------------------------------
R11 proved the soft-router *mechanism* works where regimes separate (SOL: the
only positive DSR in the run, +0.370). The bottleneck was that a *per-asset*
HMM on [own return, own 20d vol] degenerated on BTC and ETH (no vol separation,
bear-return > neutral-return). R12 changes the regime signal:

  1. CROSS-ASSET market-structure features instead of per-asset returns+vol.
  2. ONE shared regime learned at the market level, applied to all three
     instruments — so BTC/ETH cannot each degenerate independently; they inherit
     the (hopefully cleaner) market regime.

If BTC/ETH then inherit a separated regime, their combo DSR may turn positive
like SOL's, and the combo may clear the gate.

Data reality (honest caveat)
----------------------------
The originally-specified crypto-native features — funding-rate history and true
BTC-dominance / total-mcap — are NOT obtainable in this environment:
  - market_data.funding_rates covers only 2026-03 → 2026-06 (3 months).
  - OKX API 403, Binance 451, CoinGecko historical 401 (all geo/key blocked).
So R12 uses the executable core of the idea: market-structure features derived
from the three majors' OHLCV (which dominate crypto mcap anyway):

  feat 1  basket return    = mean(ret_btc, ret_eth, ret_sol)   [market trend]
  feat 2  market vol (20d) = rolling std of basket return       [whole-mkt vol]
  feat 3  dominance proxy  = ret_btc - mean(ret_eth, ret_sol)   [risk-on/off]

Mechanism unchanged from R11 (validated): HMM 3-state, full cov, seed 42, soft
router (P(bull)·trend_long + P(neutral)·MR + P(bear)·trend_short), walk-forward
2yr window / monthly retrain, FILTERED posterior at t-1 (no look-ahead; pays the
transition lag honestly), kill-switch >3 flips/10d → ×0.5.

Gate: identical helivex gate (tools/strategy_gate). This is global trial **#8**;
the DSR selection-bias bar rises accordingly and is paid honestly.

Run:  ./venv/bin/python ops/scripts/hmm_xasset_regime_gate.py [--register]
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import warnings
from pathlib import Path

import numpy as np
import pandas as pd

PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from tools.strategy_gate import _dsr_threshold, _load_trials, _save_trial  # noqa: E402

# Reuse the validated R11 building blocks (legs, pnl, gate, diagnostics, consts)
import importlib.util  # noqa: E402
_r11_path = Path(__file__).parent / "hmm_regime_combo_gate.py"
_spec = importlib.util.spec_from_file_location("r11", _r11_path)
r11 = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(r11)

warnings.filterwarnings("ignore")

INSTRUMENTS = r11.INSTRUMENTS
PERIODS_1D  = r11.PERIODS_1D
RV_WINDOW   = r11.RV_WINDOW
TRAIN_WIN   = r11.TRAIN_WIN
RETRAIN_EV  = r11.RETRAIN_EV
N_STATES    = r11.N_STATES
HMM_ITER    = r11.HMM_ITER
SEED        = r11.SEED
KS_WINDOW   = r11.KS_WINDOW
KS_MAXFLIP  = r11.KS_MAXFLIP
KS_SCALE    = r11.KS_SCALE
N_SPLITS    = r11.N_SPLITS
EMBARGO     = r11.EMBARGO
PBO_THR     = r11.PBO_THR


# ── Cross-asset market features (one shared regime) ──────────────────────────

def build_market_features(daily_by_inst: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """Build the market-level feature frame on the union daily date index.

    feat 0 basket return    = mean of available per-asset daily returns
    feat 1 market vol (20d)  = rolling std of basket return
    feat 2 dominance proxy   = ret_btc - mean(ret_alts)   (risk-on/off direction)
    """
    rets = {}
    for inst, df in daily_by_inst.items():
        c = df["close"].astype(float)
        rets[inst] = c.pct_change()
    R = pd.DataFrame(rets).sort_index()           # columns per instrument, by date

    basket = R.mean(axis=1, skipna=True)          # market trend
    mvol = basket.rolling(RV_WINDOW).std()        # whole-market vol regime

    btc = R.get("BTC-USDT-SWAP")
    alts = R[[c for c in R.columns if c != "BTC-USDT-SWAP"]].mean(axis=1, skipna=True)
    dominance = btc - alts                          # BTC outperforms alts ⇒ risk-off

    feats = pd.DataFrame({
        "basket_ret": basket,
        "mkt_vol": mvol,
        "dominance": dominance,
    })
    return feats


def xasset_regime_posteriors(feats: pd.DataFrame) -> pd.DataFrame:
    """Walk-forward filtered posteriors for ONE shared 3-state HMM on the market
    features. Returns DataFrame indexed by date, columns [bull, neutral, bear]
    ordered by training-window basket-return mean. P at date d uses data ≤ d.
    """
    from hmmlearn.hmm import GaussianHMM

    X_all = feats.to_numpy(dtype=float)
    idx = feats.index
    n = len(idx)
    valid = ~np.isnan(X_all).any(axis=1)
    first_valid = int(np.argmax(valid))           # after RV warmup

    P = np.full((n, N_STATES), np.nan)
    model = None; mu = sd = None; order = None; last_fit = -10**9

    start = first_valid + TRAIN_WIN
    for t in range(start, n):
        if model is None or (t - last_fit) >= RETRAIN_EV:
            tr0 = t - TRAIN_WIN
            X = X_all[tr0:t]
            X = X[~np.isnan(X).any(axis=1)]
            if len(X) < 60:
                continue
            mu = X.mean(axis=0); sd = X.std(axis=0) + 1e-10
            try:
                m = GaussianHMM(n_components=N_STATES, covariance_type="full",
                                n_iter=HMM_ITER, random_state=SEED, tol=1e-4)
                m.fit((X - mu) / sd)
            except Exception:
                continue
            model, last_fit = m, t
            order = np.argsort(model.means_[:, 0])[::-1]   # [bull, neutral, bear]
        if model is None:
            continue
        d0 = max(first_valid, t - TRAIN_WIN)
        Xd = X_all[d0:t]
        Xd = Xd[~np.isnan(Xd).any(axis=1)]
        if len(Xd) < 2:
            continue
        try:
            post = model.predict_proba((Xd - mu) / sd)[-1]
        except Exception:
            continue
        P[t] = post[order]
    return pd.DataFrame(P, index=idx, columns=["bull", "neutral", "bear"])


def shared_regime_quality(Pdf: pd.DataFrame, basket_ret: pd.Series) -> dict:
    """Separation of the single shared regime, measured against the MARKET
    basket return (what it was trained on)."""
    reg = Pdf.to_numpy()
    valid = ~np.isnan(reg).any(axis=1)
    lab = np.full(len(reg), -1)
    lab[valid] = reg[valid].argmax(axis=1)
    br = basket_ret.reindex(Pdf.index).to_numpy()
    names = ["bull", "neutral", "bear"]
    q = {}
    for s, nm in enumerate(names):
        m = lab == s
        if m.sum() < 5:
            q[nm] = {"n": int(m.sum()), "ret": None, "vol": None, "frac": 0.0}
            continue
        q[nm] = {
            "n": int(m.sum()), "frac": round(float(m.mean()), 3),
            "ret": round(float(np.nanmean(br[m]) * PERIODS_1D), 3),
            "vol": round(float(np.nanstd(br[m]) * np.sqrt(PERIODS_1D)), 3),
        }
    means = [q[nm]["ret"] for nm in names if q[nm]["ret"] is not None]
    q["_spread"] = round(max(means) - min(means), 3) if len(means) > 1 else 0.0
    q["_monotone"] = bool(len(means) == 3 and q["bull"]["ret"] > q["neutral"]["ret"] > q["bear"]["ret"])
    return q


# ── Per-instrument combo using the SHARED regime ─────────────────────────────

def combo_for_instrument(daily: pd.DataFrame, Pdf: pd.DataFrame) -> tuple[np.ndarray, dict]:
    closes = daily["close"].to_numpy(dtype=float)
    volumes = daily["volume"].to_numpy(dtype=float)
    n = len(closes)

    donch = r11.donchian_position(closes)
    mr = r11.vwap_mr_position(closes, volumes)
    long_leg = np.maximum(donch, 0.0)
    short_leg = np.minimum(donch, 0.0)

    # Align shared regime to this instrument's dates, then 1-bar lag.
    Pa = Pdf.reindex(daily.index).to_numpy()        # (n, 3) [bull,neutral,bear]
    Puse = np.full_like(Pa, np.nan); Puse[1:] = Pa[:-1]
    valid = ~np.isnan(Puse).any(axis=1)
    pb, pn, pr = Puse[:, 0], Puse[:, 1], Puse[:, 2]

    combo = np.zeros(n)
    combo[valid] = (pb[valid] * long_leg[valid]
                    + pn[valid] * mr[valid]
                    + pr[valid] * short_leg[valid])

    # kill-switch on shared-regime argmax flips (causal, 1-bar lag)
    reg = np.full(n, -1)
    vP = ~np.isnan(Pa).any(axis=1)
    reg[vP] = Pa[vP].argmax(axis=1)
    ks = np.zeros(n, dtype=bool)
    for t in range(n):
        seg = reg[max(0, t - KS_WINDOW):t]; seg = seg[seg >= 0]
        if len(seg) > 1 and int((np.diff(seg) != 0).sum()) > KS_MAXFLIP:
            ks[t] = True
    ks_use = np.zeros(n, dtype=bool); ks_use[1:] = ks[:-1]
    combo[ks_use] *= KS_SCALE

    valid_from = int(np.argmax(valid)) if valid.any() else n
    meta = {"valid_from": valid_from, "ks_frac": float(ks_use.mean()),
            "closes": closes}
    return combo, meta


# ── Main ─────────────────────────────────────────────────────────────────────

async def main(register: bool) -> None:
    print("=" * 76)
    print("R12 — Cross-Asset Single Regime (final HMM attempt)")
    print("=" * 76)
    print("CAVEAT: funding/true-dominance history unavailable (APIs blocked, "
          "funding table=3mo).\n        Using OHLCV-derived market-structure "
          "proxies (basket ret, mkt vol, BTC-vs-alt).\n")

    trial_n = _load_trials()["total_trials"] + 1
    dsr_thr = _dsr_threshold(trial_n)
    print(f"Global trial #{trial_n}   DSR selection-bias bar: {dsr_thr:.3f}")
    print(f"Gate: CPCV n_splits={N_SPLITS}, embargo={EMBARGO}, PBO<{PBO_THR}, "
          f"cost={r11.COST_BPS}bps/side, periods/yr={PERIODS_1D}\n")

    daily_by_inst = {inst: await r11.load_daily(inst) for inst in INSTRUMENTS}

    feats = build_market_features(daily_by_inst)
    Pdf = xasset_regime_posteriors(feats)
    rq = shared_regime_quality(Pdf, feats["basket_ret"])

    print("── SHARED cross-asset regime quality (vs market basket return) ──")
    print(f"   {'state':8s} {'ann.ret':>8s} {'ann.vol':>8s} {'frac':>6s} {'n':>6s}")
    for nm in ("bull", "neutral", "bear"):
        r = rq[nm]
        if r["ret"] is None:
            print(f"   {nm:8s} degenerate (n={r['n']})")
        else:
            print(f"   {nm:8s} {r['ret']:>+8.2f} {r['vol']:>8.2f} {r['frac']:>6.2f} {r['n']:>6d}")
    print(f"   return spread bull−bear: {rq['_spread']:+.2f}   "
          f"monotone(bull>neut>bear): {rq['_monotone']}")
    valid_dates = Pdf.dropna().index
    if len(valid_dates):
        print(f"   shared regime active: {valid_dates[0].date()} → {valid_dates[-1].date()}\n")

    # R11 per-asset combo DSR for side-by-side (from registered trial #7).
    r11_combo_dsr = {"BTC-USDT-SWAP": -3.550, "ETH-USDT-SWAP": -0.774, "SOL-USDT-SWAP": 0.370}

    results = {}
    print(f"── Per-instrument combo (SHARED regime) ──")
    print(f"   {'inst':14s} {'gross':>7s} {'meanOOS':>8s} {'foldStd':>8s} "
          f"{'DSR':>7s} {'adjDSR':>7s} {'PBO':>5s}  {'R11 DSR':>8s}  verdict")
    for inst in INSTRUMENTS:
        daily = daily_by_inst[inst]
        combo, meta = combo_for_instrument(daily, Pdf)
        g = r11.gate_one(combo, meta["closes"], meta["valid_from"])
        adj = g["deflated_sharpe"] - dsr_thr
        status = "PASS" if (not g["fail_reasons"] and adj > 0) else "FAIL"
        g["adjusted_dsr"] = adj; g["status_adj"] = status
        g["ks_frac"] = meta["ks_frac"]
        results[inst] = g
        fstd = float(np.std(g["oos_sharpes"])) if g["oos_sharpes"] else float("nan")
        print(f"   {inst:14s} {g['gross_sharpe']:>7.3f} {g['mean_oos_sharpe']:>8.3f} "
              f"{fstd:>8.3f} {g['deflated_sharpe']:>7.3f} {adj:>7.3f} {g['pbo']:>5.2f}  "
              f"{r11_combo_dsr[inst]:>+8.3f}  {status}")

    overall = "PASS" if all(results[i]["status_adj"] == "PASS" for i in INSTRUMENTS) else "FAIL"

    print("\n── R11 per-asset vs R12 cross-asset combo (did BTC/ETH DSR turn?) ──")
    for inst in INSTRUMENTS:
        d11 = r11_combo_dsr[inst]; d12 = results[inst]["deflated_sharpe"]
        arrow = "↑" if d12 > d11 else "↓"
        turned = "turned POSITIVE" if (d11 <= 0 and d12 > 0) else ("still negative" if d12 <= 0 else "stayed positive")
        print(f"   {inst:14s} R11={d11:+.3f}  →  R12={d12:+.3f}  {arrow}  ({turned})")

    print("\nDSR-bar sensitivity (honest extra-DoF accounting):")
    for neff in (trial_n, 12, 20):
        thr = _dsr_threshold(neff)
        passes = sum(1 for i in INSTRUMENTS
                     if results[i]["deflated_sharpe"] > thr and not results[i]["fail_reasons"])
        print(f"   N_eff={neff:3d}  threshold={thr:.3f}  combo passes {passes}/3")

    print("\n" + "=" * 76)
    print(f"R12 CROSS-ASSET COMBO OVERALL VERDICT: {overall}")
    print("=" * 76)

    if register:
        metrics = {
            "instruments": {inst: {
                "status": results[inst]["status_adj"],
                "dsr": results[inst]["deflated_sharpe"],
                "pbo": results[inst]["pbo"],
                "mean_oos": results[inst]["mean_oos_sharpe"],
                "gross_sharpe": results[inst]["gross_sharpe"],
            } for inst in INSTRUMENTS},
            "overall": overall,
            "note": "R12 cross-asset single regime (OHLCV market-structure proxies; "
                    "funding/dominance history unavailable)",
        }
        tn = _save_trial("ops/scripts/hmm_xasset_regime_gate.py (R12)", overall, metrics)
        print(f"\nRegistered as global trial #{tn} in .gate_trials.json")
    else:
        print("\n(dry run — not registered; pass --register to record the trial)")

    return {"overall": overall, "rq": rq, "results": results, "trial_n": trial_n}


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--register", action="store_true")
    args = ap.parse_args()
    asyncio.run(main(args.register))
