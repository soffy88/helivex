#!/usr/bin/env python
"""tools/strategy_gate.py — Config-driven backtest gate.

Usage:
    python tools/strategy_gate.py --config strategies/trend_dual.yaml [--instrument BTC-USDT-SWAP]

What it does:
    1. Load YAML strategy config
    2. Fetch OHLCV from TimescaleDB; resample as needed
    3. Run the matching omodul strategy to get per-bar signals
    4. CPCV walk-forward: split → compute OOS Sharpe per fold
    5. DSR (Deflated Sharpe Ratio) + PBO (Probability of Backtest Overfitting)
    6. DSR threshold adjusted by global trial count (selection bias correction)
    7. Print PASS/FAIL verdict with reasons

Global trial count:
    Stored in .gate_trials.json in the project root.
    DSR threshold for N trials ≈ expected max(SR_1,...,SR_N) from random walk,
    following Bailey & López de Prado (2016).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import sys
from pathlib import Path

import asyncpg
import numpy as np
import yaml

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT.parent.parent / "platform" / "3O" / "oprim"))
sys.path.insert(0, str(PROJECT_ROOT.parent.parent / "platform" / "3O" / "oskill"))
sys.path.insert(0, str(PROJECT_ROOT.parent.parent / "platform" / "3O" / "omodul"))

TRIAL_FILE = PROJECT_ROOT / ".gate_trials.json"
DB_DSN     = "postgresql://helios:helios_dev_pass@localhost:5434/helivex"

STRATEGY_MAP = {
    "trend_dual":   ("omodul.strategies.trend_dual",   "trend_dual"),
    "vwap_mr_dual": ("omodul.strategies.vwap_mr_dual", "vwap_mr_dual"),
    "spot_trend":   ("omodul.strategies.spot_trend",   "spot_trend"),
}


# ──────────────────── Trial counter ────────────────────

def _load_trials() -> dict:
    if TRIAL_FILE.exists():
        with open(TRIAL_FILE) as f:
            return json.load(f)
    return {"total_trials": 0, "history": []}


def _save_trial(config_path: str, verdict: str, metrics: dict) -> int:
    data = _load_trials()
    data["total_trials"] += 1
    data["history"].append({
        "trial_n": data["total_trials"],
        "config":  config_path,
        "verdict": verdict,
        "metrics": metrics,
    })
    with open(TRIAL_FILE, "w") as f:
        json.dump(data, f, indent=2)
    return data["total_trials"]


def _dsr_threshold(n_trials: int) -> float:
    """Expected max Sharpe for n_trials iid standard-normal draws.

    Approximation: E[max(Z_1,...,Z_N)] ≈ (1-γ)*Φ⁻¹(1-1/N) + γ*Φ⁻¹(1-1/(N*e))
    where γ ≈ 0.5772 (Euler-Mascheroni). Returns 0.0 for N<=1.

    Bailey, D.H. & López de Prado, M. (2016). The Deflated Sharpe Ratio.
    """
    if n_trials <= 1:
        return 0.0
    from scipy import stats
    gamma = 0.5772
    e     = math.e
    p1    = 1.0 - 1.0 / n_trials
    p2    = 1.0 - 1.0 / (n_trials * e)
    p1    = max(0.001, min(0.999, p1))
    p2    = max(0.001, min(0.999, p2))
    return (1.0 - gamma) * stats.norm.ppf(p1) + gamma * stats.norm.ppf(p2)


# ──────────────────── DB fetch ────────────────────

async def _fetch_ohlcv(instrument: str, db_source: str) -> dict[str, np.ndarray]:
    conn = await asyncpg.connect(DB_DSN)
    rows = await conn.fetch(
        """SELECT bar_close_ts,
                  open::float, high::float, low::float, close::float,
                  volume::float
           FROM market_data.ohlcv_1h
           WHERE instrument=$1 AND source=$2
           ORDER BY bar_close_ts""",
        instrument, db_source,
    )
    await conn.close()
    if not rows:
        raise ValueError(f"No data for instrument={instrument!r} source={db_source!r}")
    return {
        "ts":     np.array([r[0] for r in rows]),
        "open":   np.array([r[1] for r in rows], dtype=float),
        "high":   np.array([r[2] for r in rows], dtype=float),
        "low":    np.array([r[3] for r in rows], dtype=float),
        "close":  np.array([r[4] for r in rows], dtype=float),
        "volume": np.array([r[5] for r in rows], dtype=float),
    }


def _resample_ohlcv(raw: dict, bars_per_target: int) -> dict:
    """Resample 1H OHLCV to N-hour OHLCV by grouping consecutive bars."""
    if bars_per_target <= 1:
        return raw
    c, h, l, o, v = (raw["close"], raw["high"], raw["low"], raw["open"], raw["volume"])
    n = len(c)
    result_o, result_h, result_l, result_c, result_v = [], [], [], [], []
    for start in range(0, n - bars_per_target + 1, bars_per_target):
        blk = slice(start, start + bars_per_target)
        result_o.append(o[start])
        result_h.append(float(np.max(h[blk])))
        result_l.append(float(np.min(l[blk])))
        result_c.append(c[start + bars_per_target - 1])
        result_v.append(float(np.sum(v[blk])))
    return {
        "open":   np.array(result_o),
        "high":   np.array(result_h),
        "low":    np.array(result_l),
        "close":  np.array(result_c),
        "volume": np.array(result_v),
    }


def _resample_to_1d(raw: dict) -> dict:
    """Resample OHLCV to 1D using pandas groupby on date."""
    import pandas as pd
    ts = raw["ts"]
    # asyncpg returns datetime.datetime objects; handle both datetime and numeric
    if len(ts) > 0 and hasattr(ts[0], "date"):
        dates = [t.date() for t in ts]
    else:
        ts_arr = np.asarray(ts, dtype=float)
        unit = "ns" if float(ts_arr[0]) > 1e15 else "s"
        dates = pd.to_datetime(ts_arr, unit=unit, utc=True).date
    df = pd.DataFrame({
        "date":   dates,
        "open":   raw["open"],
        "high":   raw["high"],
        "low":    raw["low"],
        "close":  raw["close"],
        "volume": raw["volume"],
    })
    daily = df.groupby("date").agg(
        open=("open",   "first"),
        high=("high",   "max"),
        low=("low",     "min"),
        close=("close", "last"),
        volume=("volume", "sum"),
    ).reset_index()
    return {
        "open":   daily["open"].to_numpy(dtype=float),
        "high":   daily["high"].to_numpy(dtype=float),
        "low":    daily["low"].to_numpy(dtype=float),
        "close":  daily["close"].to_numpy(dtype=float),
        "volume": daily["volume"].to_numpy(dtype=float),
    }


# ──────────────────── Signal → P&L ────────────────────

def _signals_to_pnl(
    signals: np.ndarray,
    closes: np.ndarray,
    cost_bps: float,
    direction: str = "both",
) -> np.ndarray:
    """Convert signal array to per-bar P&L series.

    Signal semantics:
      direction='both':  +1=enter long, -1=enter short, 0=hold current
      direction='long':  +1=enter long, -1=exit/flatten, 0=hold current

    Returns per-bar returns net of cost.
    """
    n = len(closes)
    pnl = np.zeros(n)
    position = 0
    entry_price = 0.0
    cost_frac = cost_bps / 10_000.0

    for i in range(n - 1):
        sig = int(signals[i])

        # Determine desired new position
        if direction == "long":
            if sig == 1:
                new_pos = 1
            elif sig == -1:
                new_pos = 0   # exit signal → flatten
            else:
                new_pos = position  # hold
        else:  # 'both'
            if sig != 0:
                new_pos = sig
            else:
                new_pos = position

        if new_pos != position:
            # Close existing
            if position != 0:
                ret = position * (closes[i] - entry_price) / (entry_price + 1e-10)
                pnl[i] += ret - cost_frac
                position = 0
            # Open new
            if new_pos != 0:
                position    = new_pos
                entry_price = closes[i]
                pnl[i] -= cost_frac

        # Mark to market
        if position != 0 and i < n - 1:
            pnl[i + 1] += position * (closes[i + 1] - closes[i]) / (closes[i] + 1e-10)

    return pnl


# ──────────────────── CPCV gate ────────────────────

def _sharpe(pnl: np.ndarray, periods_per_year: int) -> float:
    if len(pnl) < 2 or float(np.std(pnl)) < 1e-12:
        return 0.0
    return float(np.mean(pnl)) / float(np.std(pnl)) * math.sqrt(periods_per_year)


def _walk_forward_gate(
    pnl: np.ndarray,
    n_splits: int,
    embargo_bars: int,
    periods_per_year: int,
    pbo_threshold: float = 0.5,
) -> dict:
    """Combinatorial Purged Cross-Validation gate.

    IS/OOS split: each fold splits data into first 2/3 (IS) + last 1/3 (OOS).
    PBO: fraction of splits where IS Sharpe > all OOS Sharpes.
    DSR: mean OOS Sharpe adjusted for # folds.
    """
    n = len(pnl)
    fold_size = (n - embargo_bars * (n_splits - 1)) // n_splits
    if fold_size < 50:
        return {
            "oos_sharpes": [], "is_sharpes": [],
            "mean_oos_sharpe": float("nan"),
            "pbo": float("nan"),
            "deflated_sharpe": float("nan"),
            "fail_reasons": ["insufficient data for gate"],
            "status": "FAIL",
        }

    oos_sharpes = []
    is_sharpes  = []
    pbo_count   = 0

    for k in range(n_splits):
        start = k * (fold_size + embargo_bars)
        end   = min(start + fold_size, n)
        if end - start < 20:
            continue

        fold_pnl = pnl[start:end]
        split    = max(1, int(len(fold_pnl) * 2 / 3))
        is_pnl   = fold_pnl[:split]
        oos_pnl  = fold_pnl[split:]

        is_sr  = _sharpe(is_pnl,  periods_per_year)
        oos_sr = _sharpe(oos_pnl, periods_per_year)
        is_sharpes.append(is_sr)
        oos_sharpes.append(oos_sr)
        if is_sr > oos_sr:
            pbo_count += 1

    if not oos_sharpes:
        return {
            "oos_sharpes": [], "is_sharpes": [],
            "mean_oos_sharpe": float("nan"),
            "pbo": float("nan"),
            "deflated_sharpe": float("nan"),
            "fail_reasons": ["no valid folds"],
            "status": "FAIL",
        }

    mean_oos = float(np.mean(oos_sharpes))
    pbo      = pbo_count / len(oos_sharpes)

    # DSR: adjust by fold variance (Bailey & López de Prado 2016 simplified)
    n_f       = len(oos_sharpes)
    oos_std   = float(np.std(oos_sharpes)) if n_f > 1 else 0.0
    dsr       = mean_oos - oos_std  # penalize high variance across folds

    fail_reasons = []
    if dsr <= 0:
        fail_reasons.append(f"DSR={dsr:.3f} ≤ 0")
    if pbo >= pbo_threshold:
        fail_reasons.append(f"PBO={pbo:.2f} ≥ {pbo_threshold}")

    status = "PASS" if not fail_reasons else "FAIL"

    return {
        "oos_sharpes":      oos_sharpes,
        "is_sharpes":       is_sharpes,
        "mean_oos_sharpe":  mean_oos,
        "pbo":              pbo,
        "deflated_sharpe":  dsr,
        "fail_reasons":     fail_reasons,
        "status":           status,
    }


# ──────────────────── Main gate runner ────────────────────

def _periods_per_year(timeframe: str) -> int:
    tf = timeframe.upper()
    if tf == "1H":   return 8760
    if tf == "4H":   return 8760 // 4
    if tf == "1D":   return 365
    if tf == "30M":  return 17520
    return 8760


async def run_gate(config_path: str, instrument: str | None = None, verbose: bool = True) -> dict:
    cfg_file = PROJECT_ROOT / config_path
    with open(cfg_file) as f:
        cfg = yaml.safe_load(f)

    strategy_name = cfg["strategy"]
    timeframe     = cfg.get("timeframe", "1H")
    db_source     = cfg.get("db_source", "okx_swap_1h")
    instruments   = [instrument] if instrument else cfg.get("instruments", [])
    if not instruments:
        raise ValueError("No instruments specified in config or via --instrument")

    gate_cfg       = cfg.get("gate", {})
    n_splits       = int(gate_cfg.get("n_splits", 6))
    embargo_bars   = int(gate_cfg.get("embargo_bars", 50))
    pbo_threshold  = float(gate_cfg.get("pbo_threshold", 0.5))
    ppy            = _periods_per_year(timeframe)

    # Load strategy function
    if strategy_name not in STRATEGY_MAP:
        raise ValueError(f"Unknown strategy: {strategy_name!r}. Known: {list(STRATEGY_MAP)}")
    mod_path, fn_name = STRATEGY_MAP[strategy_name]
    import importlib
    mod = importlib.import_module(mod_path)
    strategy_fn = getattr(mod, fn_name)

    # Global trial count (before this run)
    trials_before = _load_trials()["total_trials"]
    dsr_threshold = _dsr_threshold(trials_before + 1)

    if verbose:
        print(f"\n{'='*60}")
        print(f"R7 strategy_gate: {strategy_name}")
        print(f"Config : {config_path}")
        print(f"Trial  : #{trials_before + 1}  (DSR threshold for selection bias: {dsr_threshold:.3f})")
        print(f"{'='*60}")

    all_results = {}

    for inst in instruments:
        if verbose:
            print(f"\n── Instrument: {inst} ──")

        # Fetch OHLCV
        try:
            raw = await _fetch_ohlcv(inst, db_source)
        except Exception as e:
            print(f"  [SKIP] DB fetch failed: {e}")
            all_results[inst] = {"status": "SKIP", "reason": str(e)}
            continue

        # Resample
        if cfg.get("resample_to_1d"):
            ohlcv = _resample_to_1d(raw)
        elif cfg.get("resample_bars", 1) > 1:
            ohlcv = _resample_ohlcv(raw, int(cfg["resample_bars"]))
        elif cfg.get("resample_from_1h", 1) > 1:
            ohlcv = _resample_ohlcv(raw, int(cfg["resample_from_1h"]))
        else:
            ohlcv = {k: raw[k] for k in ("open", "high", "low", "close", "volume")}

        n_bars = len(ohlcv["close"])
        if verbose:
            print(f"  Bars: {n_bars} @ {timeframe}")

        # Run strategy → signals
        market_state = {
            "ohlcv": ohlcv, "instrument": inst,
            "current_positions": {}, "capital_usd": 10000.0,
        }
        result = strategy_fn(market_state, cfg)
        signals  = result["signals"]
        cost_bps = result["cost_bps"]

        if verbose:
            print(f"  Signals: {int(np.sum(signals != 0))} / {n_bars} bars  "
                  f"({int(np.sum(signals==1))} long, {int(np.sum(signals==-1))} short)")

        # P&L — direction from config
        closes    = ohlcv["close"]
        direction = cfg.get("signal_logic", {}).get("direction", "both")
        pnl       = _signals_to_pnl(signals, closes, cost_bps, direction=direction)

        gross_sr = _sharpe(pnl, ppy)
        if verbose:
            print(f"  Gross Sharpe: {gross_sr:.3f}")

        # Walk-forward CPCV gate
        gate = _walk_forward_gate(pnl, n_splits, embargo_bars, ppy, pbo_threshold)
        gate["gross_sharpe"] = gross_sr

        # Apply DSR correction for global N trials
        adjusted_dsr = gate["deflated_sharpe"] - dsr_threshold
        gate["adjusted_dsr"]   = adjusted_dsr
        gate["dsr_threshold"]  = dsr_threshold
        gate["trial_n"]        = trials_before + 1

        # Re-check PASS with adjusted threshold
        if not math.isnan(gate["deflated_sharpe"]) and gate["deflated_sharpe"] > 0 and adjusted_dsr <= 0:
            gate["fail_reasons"].append(f"DSR={gate['deflated_sharpe']:.3f} > 0 but adjusted_DSR={adjusted_dsr:.3f} ≤ 0 (N={trials_before+1} trials)")
            gate["status"] = "FAIL"

        if verbose:
            print(f"  OOS Sharpes: {[f'{s:.3f}' for s in gate['oos_sharpes']]}")
            print(f"  Mean OOS: {gate['mean_oos_sharpe']:.3f}  "
                  f"DSR: {gate['deflated_sharpe']:.3f}  "
                  f"Adj-DSR: {adjusted_dsr:.3f}  "
                  f"PBO: {gate['pbo']:.2f}")
            verdict_str = f"  ✓ PASS" if gate["status"] == "PASS" else f"  ✗ FAIL"
            if gate["fail_reasons"]:
                verdict_str += f" — {'; '.join(gate['fail_reasons'])}"
            print(verdict_str)

        all_results[inst] = gate

    # Overall verdict: PASS only if ALL instruments pass
    overall = "PASS" if all(v.get("status") == "PASS" for v in all_results.values()) else "FAIL"

    if verbose:
        print(f"\n{'='*60}")
        print(f"OVERALL VERDICT: {overall}")
        print(f"{'='*60}\n")

    # Save trial
    metrics = {
        "instruments": {
            inst: {
                "status":       v.get("status"),
                "dsr":          v.get("deflated_sharpe"),
                "pbo":          v.get("pbo"),
                "mean_oos":     v.get("mean_oos_sharpe"),
                "gross_sharpe": v.get("gross_sharpe"),
            }
            for inst, v in all_results.items()
        },
        "overall": overall,
    }
    trial_n = _save_trial(config_path, overall, metrics)

    return {
        "overall_status": overall,
        "trial_n":        trial_n,
        "instruments":    all_results,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="R7 strategy backtest gate")
    parser.add_argument("--config",     required=True, help="Path to strategy YAML (relative to project root)")
    parser.add_argument("--instrument", default=None,  help="Override instrument (default: use all in config)")
    parser.add_argument("--quiet",      action="store_true", help="Suppress verbose output")
    args = parser.parse_args()

    result = asyncio.run(run_gate(args.config, args.instrument, verbose=not args.quiet))

    # Exit code: 0=PASS, 1=FAIL
    sys.exit(0 if result["overall_status"] == "PASS" else 1)


if __name__ == "__main__":
    main()
