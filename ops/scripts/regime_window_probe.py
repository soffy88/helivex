#!/usr/bin/env python3
"""Zero-cost test: did R11/R12 die from a too-blunt 2yr window, or wrong direction?

Same BTC/ETH/SOL OHLCV, same returns+vol features, same R11 soft-router mechanism
— only the HMM training window changes. The probe found returns+vol separated BTC
well on a recent 6mo window but R11 found it near-degenerate on a 2yr window.

Part 1 (separation, NO trial): regime quality at window ∈ {504, 180, 90} daily
  (≈2yr / 6mo / 3mo), per instrument. Does short window improve BTC/ETH separation
  vs 2yr? + OOS persistence (regime-rank ↔ next-day return corr).
Part 2 (combo gate, REGISTERED trial 11): R11 soft-router combo at the a-priori
  short window (180d = 6mo), full helivex gate (DSR/PBO/global-N). SOL was +0.37
  DSR at 2yr; if short window lifts all three, the combo may clear the gate.

A-priori: windows fixed (504/180/90; gate at 180), no tuning to pass. Reuses the
exact R11 code (mechanism unchanged).
Run:  ./venv/bin/python ops/scripts/regime_window_probe.py [--register]
"""
from __future__ import annotations

import argparse
import asyncio
import importlib.util
import sys
import warnings
from pathlib import Path

import numpy as np

warnings.filterwarnings("ignore")
PROJ = Path(__file__).parent.parent.parent
sys.path.insert(0, str(PROJ))
from tools.strategy_gate import _dsr_threshold, _load_trials, _save_trial  # noqa: E402

_spec = importlib.util.spec_from_file_location("r11", Path(__file__).parent / "hmm_regime_combo_gate.py")
r11 = importlib.util.module_from_spec(_spec); _spec.loader.exec_module(r11)

INSTRUMENTS = r11.INSTRUMENTS
WINDOWS = [504, 180, 90]   # 2yr / 6mo / 3mo (daily)
GATE_WIN = 180             # a-priori short window for the registered gate run


def oos_persistence(P: np.ndarray, closes: np.ndarray) -> float:
    """corr(regime rank used at t [bull=2..bear=0, from filtered posterior t-1],
    next-day return at t). >0 = regime predicts forward return OOS."""
    n = len(closes)
    rets = np.zeros(n); rets[1:] = np.diff(closes) / (closes[:-1] + 1e-10)
    Puse = np.full_like(P, np.nan); Puse[1:] = P[:-1]
    valid = ~np.isnan(Puse).any(axis=1)
    if valid.sum() < 20:
        return float("nan")
    rank = 2 - Puse[valid].argmax(axis=1)      # col0=bull→rank2
    r = rets[valid]
    if np.std(rank) < 1e-9:
        return 0.0
    return float(np.corrcoef(rank, r)[0, 1])


async def main(register: bool) -> None:
    print("=" * 80)
    print("Window validation — R11 returns+vol regime at 2yr / 6mo / 3mo (BTC/ETH/SOL)")
    print("=" * 80)
    daily = {inst: await r11.load_daily(inst) for inst in INSTRUMENTS}

    # ── Part 1: separation vs window ──
    print("\n── Part 1: regime separation by window (returns+vol, R11 features) ──")
    print(f"{'inst':14} {'window':>7} {'bull':>7} {'neut':>7} {'bear':>7} {'spread':>7} {'mono':>5} {'OOScorr':>8} {'nOOS':>6}")
    for inst in INSTRUMENTS:
        closes = daily[inst]["close"].to_numpy(float)
        for W in WINDOWS:
            r11.TRAIN_WIN = W
            P, _ = r11.regime_posteriors(closes)
            q = r11.regime_quality(P, closes)
            corr = oos_persistence(P, closes)
            b = q["bull"]["mean_ret_ann"]; nu = q["neutral"]["mean_ret_ann"]; be = q["bear"]["mean_ret_ann"]
            noos = int((~np.isnan(P).any(axis=1)).sum())
            fmt = lambda x: f"{x:+.2f}" if x is not None else "  n/a"
            print(f"{inst:14} {W:>7} {fmt(b):>7} {fmt(nu):>7} {fmt(be):>7} "
                  f"{(q['_ret_spread'] if q['_ret_spread'] is not None else 0):>+7.2f} "
                  f"{str(q.get('_monotone', False)):>5} {corr:>+8.3f} {noos:>6}")
        print()

    # ── Part 2: soft-router combo at GATE_WIN through the full gate (trial 11) ──
    r11.TRAIN_WIN = GATE_WIN
    trials_before = _load_trials()["total_trials"]
    trial_n = trials_before + 1
    dsr_thr = _dsr_threshold(trial_n)
    print("=" * 80)
    print(f"── Part 2: soft-router combo @ {GATE_WIN}d window — FULL GATE (trial #{trial_n}, bar {dsr_thr:.3f}) ──")
    print("=" * 80)
    r11_2yr = {"BTC-USDT-SWAP": -3.550, "ETH-USDT-SWAP": -0.774, "SOL-USDT-SWAP": 0.370}
    results = {}
    print(f"{'inst':14} {'gross':>7} {'meanOOS':>8} {'foldStd':>8} {'DSR':>7} {'adjDSR':>7} {'PBO':>5} {'2yrDSR':>8}  verdict")
    for inst in INSTRUMENTS:
        v = r11.build_variants(daily[inst])
        g = r11.gate_one(v["positions"]["regime_combo"], v["_closes"], v["_valid_from"])
        adj = g["deflated_sharpe"] - dsr_thr
        status = "PASS" if (not g["fail_reasons"] and adj > 0) else "FAIL"
        g["status_adj"] = status; results[inst] = g
        fstd = float(np.std(g["oos_sharpes"])) if g["oos_sharpes"] else float("nan")
        print(f"{inst:14} {g['gross_sharpe']:>7.3f} {g['mean_oos_sharpe']:>8.3f} {fstd:>8.3f} "
              f"{g['deflated_sharpe']:>7.3f} {adj:>7.3f} {g['pbo']:>5.2f} {r11_2yr[inst]:>+8.3f}  {status}")
    overall = "PASS" if all(results[i]["status_adj"] == "PASS" for i in INSTRUMENTS) else "FAIL"
    print(f"\n{'='*80}\nSHORT-WINDOW ({GATE_WIN}d) SOFT-ROUTER COMBO VERDICT (N={trial_n}): {overall}\n{'='*80}")

    if register:
        metrics = {"instruments": {inst: {
            "status": results[inst]["status_adj"], "dsr": results[inst]["deflated_sharpe"],
            "pbo": results[inst]["pbo"], "mean_oos": results[inst]["mean_oos_sharpe"],
            "gross_sharpe": results[inst]["gross_sharpe"]} for inst in INSTRUMENTS},
            "overall": overall, "note": f"R11 soft-router combo @ {GATE_WIN}d short window (returns+vol regime)"}
        tn = _save_trial(f"ops/scripts/regime_window_probe.py (R11 combo @{GATE_WIN}d)", overall, metrics)
        print(f"\nRegistered as global trial #{tn}")
    else:
        print("\n(dry run — not registered; pass --register to record trial)")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(); ap.add_argument("--register", action="store_true")
    asyncio.run(main(ap.parse_args().register))
