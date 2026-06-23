#!/usr/bin/env python3
"""R11 — HMM Regime-Adaptive Combination of the Three helivex Strategies.

Thesis under test
-----------------
Every helivex strategy fails the gate solo because each only earns in *one*
regime and bleeds in the others, inflating cross-fold variance. This script
asks: can an HMM regime router *combine* the three real strategy signals into
one return stream that is smoother (lower fold variance) and finally clears the
helivex gate?

Difference from R4 (chop_filter_gate.py)
----------------------------------------
R4 used a 2-state HMM as an on/off *filter* (go flat in chop). That threw away
the bear-side trend profit and still failed. Here the HMM is a *strategy router*
with **soft** (probability-weighted) switching, not a hard gate:

    pos_t = P(bull)·trend_long  +  P(neutral)·mean_reversion  +  P(bear)·trend_short

  - bull  regime → trend strategy LONG side   (Donchian 20/10, from donchian_4h)
  - bear  regime → trend strategy SHORT side   ("defensive" — profit the downtrend)
  - neutral regime → VWAP mean reversion       (from vwap_mr_1h)

Design choices (paper best-practice, avoiding R4's pitfalls)
------------------------------------------------------------
1. HMM **3 states** (bull / neutral / bear), not R4's 2. The neutral buffer
   reduces whipsaw vs a hard bull/bear flip.
2. Features = [daily return, 20-day realized vol]  — the two features validated
   in the regime literature. R4 used [directional-efficiency, |bar return|].
3. hmmlearn GaussianHMM, covariance_type='full', random_state fixed (42).
4. **Soft** switching: weights are the regime posteriors, not a 0/1 mask.
5. Strict walk-forward, **no look-ahead**:
     - 2-year trailing training window, monthly (21-bar) retrain.
     - Regime for trading bar t uses the **filtered** posterior at t-1
       (predict_proba over data up to t-1, take last row). The filtered
       posterior naturally *lags* a true regime change — this is exactly the
       "5-15 day transition lag" cost the paper warns about, and it is paid
       honestly rather than assumed away by full-sequence Viterbi smoothing.
6. Instability kill-switch: if the argmax regime flips >3x in the trailing
   10 days, scale position ×0.5 (de-risk during unstable regime detection).

Timeframe
---------
Run on DAILY bars (okx_swap 4H resampled to 1D) because the regime params
(20-day vol, monthly retrain, 2-yr window, 5-15 day lag) are all daily-native.
This is the same daily ground R6/spot_trend used. The two trend legs are the
Donchian 20/10 logic of donchian_4h; the MR leg is the z-vs-VWAP logic of
vwap_mr_1h — both ported to daily bars. spot_trend_1d is long-only Donchian =
the long side of the trend leg, so it is subsumed.

Honesty / anti-self-deception
-----------------------------
- Verdict uses the SAME helivex gate as the 6 prior trials
  (tools/strategy_gate._walk_forward_gate: CPCV folds → DSR=mean_oos−std_oos,
   PBO, threshold adjusted for the global trial count in .gate_trials.json).
- This is registered as the next global trial; the DSR bar rises accordingly.
- The regime combo adds parameter degrees of freedom (3 states × legs × soft
  weights). To not cheat, NONE of those are tuned on the data: HMM hyper-params
  are fixed a-priori, strategy params are the deployed live params, and the soft
  weights ARE the posteriors (no free knob). We additionally report verdict
  robustness against an inflated effective trial count.
- Controlled baselines on the identical daily data + identical gate:
  trend_solo, mr_solo, and a regime-blind naive 50/50 blend — so we can tell
  whether the *HMM router* adds anything beyond plain diversification.

Run:  ./venv/bin/python ops/scripts/hmm_regime_combo_gate.py [--register]
"""
from __future__ import annotations

import argparse
import asyncio
import datetime
import sys
import warnings
from pathlib import Path

import asyncpg
import numpy as np
import pandas as pd

PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# Reuse the EXACT helivex gate (same logic the 6 prior trials used).
from tools.strategy_gate import (  # noqa: E402
    _walk_forward_gate,
    _sharpe,
    _dsr_threshold,
    _load_trials,
    _save_trial,
)

warnings.filterwarnings("ignore")  # silence hmmlearn convergence chatter

# ── Constants ───────────────────────────────────────────────────────────────
DB_DSN      = "postgresql://helios:helios_dev_pass@localhost:5434/helivex"
SOURCE_4H   = "okx_swap"
INSTRUMENTS = ["BTC-USDT-SWAP", "ETH-USDT-SWAP", "SOL-USDT-SWAP"]

PERIODS_1D  = 365          # daily annualisation
COST_BPS    = 5.0          # per side; 10bps round-trip (taker, matches R6)

# Strategy params = deployed live params (paper/strategies/*)
DONCH_ENTER = 20
DONCH_EXIT  = 10
VWAP_N      = 4            # vwap_mr_1h.VwapMR1HConfig.vwap_n
Z_THR       = 2.0
MR_HOLD     = 6

# HMM regime params (fixed a-priori, paper best-practice)
N_STATES    = 3
RV_WINDOW   = 20          # 20-day realized vol
TRAIN_WIN   = 504         # ~2 trading years
RETRAIN_EV  = 21          # monthly retrain
HMM_ITER    = 200
SEED        = 42

# Kill switch
KS_WINDOW   = 10          # trailing days
KS_MAXFLIP  = 3           # >3 regime flips → de-risk
KS_SCALE    = 0.5

# Gate params (same defaults as tools/strategy_gate.py)
N_SPLITS    = 6
EMBARGO     = 50
PBO_THR     = 0.5


# ── Data ────────────────────────────────────────────────────────────────────

async def load_daily(inst: str) -> pd.DataFrame:
    conn = await asyncpg.connect(DB_DSN)
    rows = await conn.fetch(
        """SELECT bar_close_ts, open::float, high::float, low::float,
                  close::float, volume::float
           FROM market_data.ohlcv_1h
           WHERE instrument=$1 AND source=$2
           ORDER BY bar_close_ts""",
        inst, SOURCE_4H,
    )
    await conn.close()
    df = pd.DataFrame([dict(r) for r in rows])
    df["bar_close_ts"] = pd.to_datetime(df["bar_close_ts"], utc=True)
    df = df.set_index("bar_close_ts").sort_index()
    daily = df.resample("1D").agg(
        open=("open", "first"), high=("high", "max"), low=("low", "min"),
        close=("close", "last"), volume=("volume", "sum"),
    ).dropna(subset=["close"])
    return daily


# ── Strategy legs (causal; pos[t] decided from info ≤ t-1) ───────────────────

def donchian_position(closes: np.ndarray) -> np.ndarray:
    """Dual-direction Donchian 20/10 position held during each bar.

    Faithful to donchian_4h.on_bar: enter long on break above prior-N high,
    short on break below prior-N low; exit on the 10-bar opposite extreme.
    Channels use bars strictly before the decision bar (shift=1, no look-ahead).
    pos[t] = position held during bar t, decided at close[t-1].
    """
    n = len(closes)
    pos = np.zeros(n)
    cur = 0
    for t in range(1, n):
        # decision uses close[t-1] vs channels of closes[t-1-N : t-1]
        d = t - 1
        if d < DONCH_ENTER:
            pos[t] = cur
            continue
        hi_e = closes[d - DONCH_ENTER:d].max()
        lo_e = closes[d - DONCH_ENTER:d].min()
        hi_x = closes[d - DONCH_EXIT:d].max()
        lo_x = closes[d - DONCH_EXIT:d].min()
        c = closes[d]
        if cur == 0:
            if c > hi_e:
                cur = 1
            elif c < lo_e:
                cur = -1
        elif cur == 1:
            if c < lo_x:
                cur = 0
        elif cur == -1:
            if c > hi_x:
                cur = 0
        pos[t] = cur
    return pos


def vwap_mr_position(closes: np.ndarray, volumes: np.ndarray) -> np.ndarray:
    """VWAP z-score mean reversion position. Faithful to vwap_mr_1h.on_bar.

    z = (close - VWAP(prior N)) / std(prior N closes). z>thr → short, z<-thr →
    long. Time-exit after MR_HOLD bars. Causal: decision at close[t-1].
    """
    n = len(closes)
    pos = np.zeros(n)
    cur = 0
    bars_left = 0
    for t in range(1, n):
        d = t - 1
        if cur != 0:
            bars_left -= 1
            if bars_left <= 0:
                cur = 0
            pos[t] = cur
            continue
        if d < VWAP_N:
            pos[t] = cur
            continue
        pc = closes[d - VWAP_N:d]
        pv = volumes[d - VWAP_N:d]
        vwap = float((pc * pv).sum() / (pv.sum() + 1e-10))
        std = float(pc.std(ddof=1)) if len(pc) > 1 else 0.0
        z = (closes[d] - vwap) / (std + 1e-10)
        if z > Z_THR:
            cur = -1; bars_left = MR_HOLD
        elif z < -Z_THR:
            cur = 1; bars_left = MR_HOLD
        pos[t] = cur
    return pos


# ── HMM regime: walk-forward filtered posteriors (no look-ahead) ─────────────

def regime_posteriors(closes: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Walk-forward 3-state HMM filtered posteriors.

    Returns (P, used_idx_start):
      P[t] = filtered posterior P(state | data ≤ t), columns ordered
             [bull, neutral, bear] by training-window mean return.
    For trading bar t we will use P[t-1] (one-bar lag), so the posterior that
    informs a trade never sees that bar's own return.
    """
    from hmmlearn.hmm import GaussianHMM

    n = len(closes)
    rets = np.zeros(n)
    rets[1:] = np.diff(closes) / (closes[:-1] + 1e-10)
    rv = pd.Series(rets).rolling(RV_WINDOW).std().to_numpy()

    P = np.full((n, N_STATES), np.nan)
    feat_start = RV_WINDOW + 1          # first bar with valid features
    model = None
    mu = sd = None
    order = None                         # state-index → [bull,neutral,bear]
    last_fit = -10**9

    for t in range(feat_start + TRAIN_WIN, n):
        # (Re)fit on the trailing 2-yr window of features ending at t-1.
        if model is None or (t - last_fit) >= RETRAIN_EV:
            tr0 = t - TRAIN_WIN
            X = np.column_stack([rets[tr0:t], rv[tr0:t]])
            X = X[~np.isnan(X).any(axis=1)]
            if len(X) < 60:
                continue
            mu = X.mean(axis=0)
            sd = X.std(axis=0) + 1e-10
            Xn = (X - mu) / sd
            try:
                m = GaussianHMM(n_components=N_STATES, covariance_type="full",
                                n_iter=HMM_ITER, random_state=SEED, tol=1e-4)
                m.fit(Xn)
            except Exception:
                continue
            model, last_fit = m, t
            # order states by mean return (feature 0, in normalised space)
            order = np.argsort(model.means_[:, 0])   # [bear, neutral, bull]
            order = order[::-1]                       # [bull, neutral, bear]

        if model is None:
            continue
        # filtered posterior at t-1: decode the window up to (and incl) t-1,
        # take the last row → P(state | data ≤ t-1).
        d0 = max(feat_start, t - TRAIN_WIN)
        Xd = np.column_stack([rets[d0:t], rv[d0:t]])
        if np.isnan(Xd).any():
            keep = ~np.isnan(Xd).any(axis=1)
            Xd = Xd[keep]
        if len(Xd) < 2:
            continue
        Xdn = (Xd - mu) / sd
        try:
            post = model.predict_proba(Xdn)[-1]      # filtered posterior at t-1
        except Exception:
            continue
        P[t] = post[order]                            # → [bull, neutral, bear]
    return P, feat_start + TRAIN_WIN


# ── PnL from a (continuous) position series ──────────────────────────────────

def pnl_from_position(pos: np.ndarray, closes: np.ndarray,
                      cost_bps_side: float = COST_BPS) -> np.ndarray:
    """pos[t] = position held during bar t. ret[t] uses close[t-1]→close[t].
    Cost charged on |Δposition| (turnover). Returns per-bar net returns.
    """
    n = len(closes)
    rets = np.zeros(n)
    rets[1:] = np.diff(closes) / (closes[:-1] + 1e-10)
    pnl = pos * rets
    turn = np.abs(np.diff(pos, prepend=0.0))
    pnl = pnl - turn * (cost_bps_side / 1e4)
    return pnl


def detection_lag(P: np.ndarray, closes: np.ndarray) -> float:
    """Median lag (days) between a 'true' trend flip (60d fwd-looking label,
    diagnostic only) and the HMM filtered-regime catching up. Informational."""
    n = len(closes)
    rets = np.zeros(n); rets[1:] = np.diff(closes) / (closes[:-1] + 1e-10)
    fwd = pd.Series(closes).pct_change(20).shift(-20).to_numpy()  # diagnostic
    true_lab = np.where(fwd > 0.05, 0, np.where(fwd < -0.05, 2, 1))
    reg = np.full(n, -1)
    valid = ~np.isnan(P).any(axis=1)
    reg[valid] = P[valid].argmax(axis=1)
    lags = []
    prev = None
    for t in range(n):
        if true_lab[t] == 1 or np.isnan(fwd[t]):
            continue
        if prev is None or true_lab[t] != prev:
            prev = true_lab[t]
            # find first bar ≥ t where reg matches
            for k in range(t, min(t + 60, n)):
                if reg[k] == true_lab[t]:
                    lags.append(k - t); break
    return float(np.median(lags)) if lags else float("nan")


# ── Build all variants for one instrument ────────────────────────────────────

def build_variants(daily: pd.DataFrame) -> dict:
    closes  = daily["close"].to_numpy(dtype=float)
    volumes = daily["volume"].to_numpy(dtype=float)
    n = len(closes)

    donch = donchian_position(closes)
    mr    = vwap_mr_position(closes, volumes)
    long_leg  = np.maximum(donch, 0.0)
    short_leg = np.minimum(donch, 0.0)

    P, used_start = regime_posteriors(closes)

    # Regime used for trading bar t = filtered posterior at t-1 (one-bar lag).
    Puse = np.full_like(P, np.nan)
    Puse[1:] = P[:-1]
    valid = ~np.isnan(Puse).any(axis=1)

    pb = Puse[:, 0]; pn = Puse[:, 1]; pr = Puse[:, 2]

    combo = np.zeros(n)
    combo[valid] = (pb[valid] * long_leg[valid]
                    + pn[valid] * mr[valid]
                    + pr[valid] * short_leg[valid])

    # Instability kill-switch: count argmax flips in trailing KS_WINDOW.
    reg = np.full(n, -1)
    vP = ~np.isnan(P).any(axis=1)
    reg[vP] = P[vP].argmax(axis=1)
    ks_active = np.zeros(n, dtype=bool)
    for t in range(n):
        lo = max(0, t - KS_WINDOW)
        seg = reg[lo:t]
        seg = seg[seg >= 0]
        if len(seg) > 1:
            flips = int((np.diff(seg) != 0).sum())
            if flips > KS_MAXFLIP:
                ks_active[t] = True
    ks_use = np.zeros(n, dtype=bool); ks_use[1:] = ks_active[:-1]
    combo_ks = combo.copy()
    combo_ks[ks_use] *= KS_SCALE

    # Regime-blind naive blend (ablation): equal weight, no HMM.
    naive = 0.5 * donch + 0.5 * mr

    # Restrict every variant to the regime-valid region so all variants and the
    # gate see the same sample (fair comparison; combo only exists there).
    sl = slice(0, n)
    out = {
        "_n": n, "_valid_from": int(np.argmax(valid)) if valid.any() else n,
        "_P": P, "_closes": closes, "_reg": reg, "_ks_frac": float(ks_use.mean()),
        "positions": {
            "trend_solo":   donch,
            "mr_solo":      mr,
            "naive_blend":  naive,
            "regime_combo": combo_ks,
        },
        "_long_frac": float((long_leg[valid] > 0).mean()) if valid.any() else 0.0,
    }
    out["_slice"] = sl
    out["_valid_mask"] = valid
    return out


def gate_one(pos: np.ndarray, closes: np.ndarray, valid_from: int) -> dict:
    """Run the helivex gate on one position series (regime-valid region)."""
    p = pos[valid_from:]
    c = closes[valid_from:]
    pnl = pnl_from_position(p, c)
    gate = _walk_forward_gate(pnl, N_SPLITS, EMBARGO, PERIODS_1D, PBO_THR)
    gate["gross_sharpe"] = _sharpe(pnl, PERIODS_1D)
    return gate


# ── Regime-quality diagnostics ───────────────────────────────────────────────

def regime_quality(P: np.ndarray, closes: np.ndarray) -> dict:
    n = len(closes)
    rets = np.zeros(n); rets[1:] = np.diff(closes) / (closes[:-1] + 1e-10)
    rv = pd.Series(rets).rolling(RV_WINDOW).std().to_numpy()
    reg = np.full(n, -1)
    valid = ~np.isnan(P).any(axis=1)
    reg[valid] = P[valid].argmax(axis=1)
    names = ["bull", "neutral", "bear"]
    q = {}
    for s, name in enumerate(names):
        m = (reg == s)
        if m.sum() < 5:
            q[name] = {"n": int(m.sum()), "mean_ret_ann": None, "vol_ann": None, "frac": 0.0}
            continue
        q[name] = {
            "n": int(m.sum()),
            "frac": round(float(m.mean()), 3),
            "mean_ret_ann": round(float(np.nanmean(rets[m]) * PERIODS_1D), 3),
            "vol_ann": round(float(np.nanmean(rv[m]) * np.sqrt(PERIODS_1D)), 3),
        }
    # separation score: spread of per-state annualised mean return
    means = [q[nm]["mean_ret_ann"] for nm in names if q[nm]["mean_ret_ann"] is not None]
    q["_ret_spread"] = round(float(max(means) - min(means)), 3) if len(means) > 1 else 0.0
    return q


# ── Main ─────────────────────────────────────────────────────────────────────

VARIANTS = ["trend_solo", "mr_solo", "naive_blend", "regime_combo"]


async def main(register: bool) -> None:
    print("=" * 74)
    print("R11 — HMM Regime-Adaptive Combination of Three Strategies")
    print("=" * 74)

    trials_before = _load_trials()["total_trials"]
    trial_n = trials_before + 1
    dsr_thr = _dsr_threshold(trial_n)
    print(f"Global trial #{trial_n}   DSR threshold (selection-bias bar): {dsr_thr:.3f}")
    print(f"Gate: CPCV n_splits={N_SPLITS}, embargo={EMBARGO}, PBO<{PBO_THR}, "
          f"cost={COST_BPS}bps/side, periods/yr={PERIODS_1D}\n")

    results: dict = {}    # inst → variant → gate
    rq_all: dict = {}
    lag_all: dict = {}
    ks_all: dict = {}

    for inst in INSTRUMENTS:
        daily = await load_daily(inst)
        v = build_variants(daily)
        vf = v["_valid_from"]
        closes = v["_closes"]
        n = v["_n"]
        print(f"── {inst}: {n} daily bars, regime-valid from bar {vf} "
              f"({daily.index[vf].date() if vf < n else 'n/a'} → {daily.index[-1].date()}), "
              f"{n - vf} OOS-eligible bars")

        rq = regime_quality(v["_P"], closes)
        rq_all[inst] = rq
        print(f"   regime quality (ann. mean ret / ann. vol / frac):")
        for nm in ("bull", "neutral", "bear"):
            r = rq[nm]
            if r["mean_ret_ann"] is None:
                print(f"     {nm:8s}: degenerate (n={r['n']})")
            else:
                print(f"     {nm:8s}: ret={r['mean_ret_ann']:+.2f}  vol={r['vol_ann']:.2f}  "
                      f"frac={r['frac']:.2f}  n={r['n']}")
        print(f"     return spread bull−bear: {rq['_ret_spread']:+.2f}  "
              f"(>0 & monotone = clean separation)")

        lag = detection_lag(v["_P"], closes)
        lag_all[inst] = lag
        ks_all[inst] = v["_ks_frac"]
        print(f"   regime detection lag (median): {lag:.0f} days   "
              f"kill-switch active: {v['_ks_frac']*100:.1f}% of bars")

        results[inst] = {}
        for var in VARIANTS:
            g = gate_one(v["positions"][var], closes, vf)
            adj = g["deflated_sharpe"] - dsr_thr
            g["adjusted_dsr"] = adj
            status = "PASS" if (not g["fail_reasons"] and adj > 0) else "FAIL"
            if g["fail_reasons"] or adj <= 0:
                status = "FAIL"
            g["status_adj"] = status
            results[inst][var] = g

        print(f"   {'variant':14s} {'gross':>7s} {'meanOOS':>8s} {'foldStd':>8s} "
              f"{'DSR':>7s} {'adjDSR':>7s} {'PBO':>5s}  verdict")
        for var in VARIANTS:
            g = results[inst][var]
            fstd = float(np.std(g["oos_sharpes"])) if g["oos_sharpes"] else float("nan")
            print(f"   {var:14s} {g['gross_sharpe']:>7.3f} {g['mean_oos_sharpe']:>8.3f} "
                  f"{fstd:>8.3f} {g['deflated_sharpe']:>7.3f} {g['adjusted_dsr']:>7.3f} "
                  f"{g['pbo']:>5.2f}  {g['status_adj']}")
        print()

    # ── Overall combo verdict (PASS only if all instruments pass) ────────────
    combo_pass = all(results[i]["regime_combo"]["status_adj"] == "PASS" for i in INSTRUMENTS)
    overall = "PASS" if combo_pass else "FAIL"

    print("=" * 74)
    print("CROSS-INSTRUMENT SUMMARY — fold variance: combo vs solo legs")
    print("=" * 74)
    print(f"{'inst':14s} {'variant':14s} {'meanOOS':>8s} {'foldStd':>8s} {'DSR':>7s} {'PBO':>5s}")
    for inst in INSTRUMENTS:
        for var in VARIANTS:
            g = results[inst][var]
            fstd = float(np.std(g["oos_sharpes"])) if g["oos_sharpes"] else float("nan")
            print(f"{inst:14s} {var:14s} {g['mean_oos_sharpe']:>8.3f} {fstd:>8.3f} "
                  f"{g['deflated_sharpe']:>7.3f} {g['pbo']:>5.2f}")

    # mean fold-std across instruments per variant — the central claim
    print("\nMean cross-fold OOS-Sharpe std (lower = smoother; the core claim):")
    for var in VARIANTS:
        stds = [float(np.std(results[i][var]["oos_sharpes"]))
                for i in INSTRUMENTS if results[i][var]["oos_sharpes"]]
        dsrs = [results[i][var]["deflated_sharpe"] for i in INSTRUMENTS]
        print(f"  {var:14s} foldStd={np.mean(stds):.3f}   meanDSR={np.mean(dsrs):+.3f}")

    # DoF-honesty: verdict robustness vs inflated effective trial count
    print("\nDSR-bar sensitivity (honest extra-DoF accounting):")
    for neff in (trial_n, 12, 20):
        thr = _dsr_threshold(neff)
        passes = sum(1 for i in INSTRUMENTS
                     if results[i]["regime_combo"]["deflated_sharpe"] > thr
                     and not results[i]["regime_combo"]["fail_reasons"])
        print(f"  N_eff={neff:3d}  threshold={thr:.3f}  combo passes {passes}/3 instruments")

    print("\n" + "=" * 74)
    print(f"REGIME-COMBO OVERALL VERDICT: {overall}")
    print("=" * 74)

    if register:
        metrics = {
            "instruments": {
                inst: {
                    "status": results[inst]["regime_combo"]["status_adj"],
                    "dsr": results[inst]["regime_combo"]["deflated_sharpe"],
                    "pbo": results[inst]["regime_combo"]["pbo"],
                    "mean_oos": results[inst]["regime_combo"]["mean_oos_sharpe"],
                    "gross_sharpe": results[inst]["regime_combo"]["gross_sharpe"],
                }
                for inst in INSTRUMENTS
            },
            "overall": overall,
            "note": "R11 HMM 3-state regime soft-router combining donchian/vwap_mr legs",
        }
        tn = _save_trial("ops/scripts/hmm_regime_combo_gate.py (R11)", overall, metrics)
        print(f"\nRegistered as global trial #{tn} in .gate_trials.json")
    else:
        print("\n(dry run — not registered; pass --register to record the trial)")

    # stash for the report writer
    return {
        "trial_n": trial_n, "dsr_thr": dsr_thr, "overall": overall,
        "results": results, "rq": rq_all, "lag": lag_all, "ks": ks_all,
    }


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--register", action="store_true",
                    help="record this run as a global trial in .gate_trials.json")
    args = ap.parse_args()
    asyncio.run(main(args.register))
