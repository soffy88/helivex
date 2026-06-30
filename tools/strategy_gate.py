#!/usr/bin/env python
"""tools/strategy_gate.py — Config-driven backtest gate.

Usage:
    python tools/strategy_gate.py --config strategies/trend_dual.yaml [--instrument BTC-USDT-SWAP]

What it does:
    1. Load YAML strategy config
    2. Fetch OHLCV from TimescaleDB; resample as needed
    3. Run the matching omodul strategy to get per-bar signals
    4. Sequential blocked walk-forward CV (embargo gap): split → OOS Sharpe per fold
    5. Two GATING heuristics + one reported diagnostic:
         - "deflated_sharpe" key  = mean_oos − std_oos  (a mean-minus-dispersion
           heuristic; NOT the skew/kurtosis-adjusted Deflated Sharpe)
         - "pbo" key              = freq(IS_sharpe > OOS_sharpe)  (an IS>OOS
           frequency; NOT the CSCV logit-rank Probability of Backtest Overfitting)
         - "deflated_sharpe_real" = the REAL Bailey & López de Prado (2016)
           Deflated Sharpe Ratio (reported only, NON-gating)
    6. The mean−std heuristic is compared against an expected-max-of-N-Sharpes
       benchmark scaled by the global trial count (legit multiple-testing
       correction; see _dsr_threshold)
    7. Print PASS/FAIL verdict with reasons

Honest-labelling note:
    Historically this file labelled the step-4 split "CPCV / combinatorial purged"
    and the step-5 outputs "DSR (Deflated Sharpe Ratio)" and "PBO". Neither label
    matched the implementation. The CV is plain sequential blocked-with-embargo
    (not combinatorial, not purged); the two metrics are the simple heuristics
    described above. Output dict/JSON keys ("deflated_sharpe", "pbo", "dsr") are
    KEPT for backward compatibility with .gate_trials.json, the gateway, and the
    web UI — only the human-facing labels/docstrings were corrected, and the real
    Deflated Sharpe is now additionally reported under "deflated_sharpe_real".

Global trial count:
    Stored in .gate_trials.json in the project root.
    The benchmark Sharpe for N trials ≈ expected max(SR_1,...,SR_N) from random
    walk, following Bailey & López de Prado (2016).
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
    # 5-minute bars live in market_data.ohlcv_5m (migrations 003/005 moved them out
    # of ohlcv_1h). Route by source so the okx_swap_5m configs keep working.
    table = "market_data.ohlcv_5m" if db_source.endswith("5m") else "market_data.ohlcv_1h"
    conn = await asyncpg.connect(DB_DSN)
    rows = await conn.fetch(
        f"""SELECT bar_close_ts,
                  open::float, high::float, low::float, close::float,
                  volume::float
           FROM {table}
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
    ts = raw.get("ts")
    n = len(c)
    result_o, result_h, result_l, result_c, result_v, result_ts = [], [], [], [], [], []
    for start in range(0, n - bars_per_target + 1, bars_per_target):
        blk = slice(start, start + bars_per_target)
        result_o.append(o[start])
        result_h.append(float(np.max(h[blk])))
        result_l.append(float(np.min(l[blk])))
        result_c.append(c[start + bars_per_target - 1])
        result_v.append(float(np.sum(v[blk])))
        if ts is not None:
            result_ts.append(ts[start + bars_per_target - 1])
    out = {
        "open":   np.array(result_o),
        "high":   np.array(result_h),
        "low":    np.array(result_l),
        "close":  np.array(result_c),
        "volume": np.array(result_v),
    }
    if ts is not None:
        out["ts"] = np.array(result_ts, dtype=object)
    return out


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
        "ts":     pd.to_datetime(ts, utc=True),
        "open":   raw["open"],
        "high":   raw["high"],
        "low":    raw["low"],
        "close":  raw["close"],
        "volume": raw["volume"],
    })
    daily = df.groupby("date").agg(
        ts=("ts",       "last"),
        open=("open",   "first"),
        high=("high",   "max"),
        low=("low",     "min"),
        close=("close", "last"),
        volume=("volume", "sum"),
    ).reset_index()
    return {
        "ts":     np.array([t.to_pydatetime() for t in daily["ts"]], dtype=object),
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
    funding_into: np.ndarray | None = None,
) -> np.ndarray:
    """Convert signal array to per-bar P&L series.

    Signal semantics:
      direction='both':  +1=enter long, -1=enter short, 0=hold current
      direction='long':  +1=enter long, -1=exit/flatten, 0=hold current

    Perp funding carry:
      ``funding_into[i]`` is the summed funding_rate of all funding events that
      occurred in the bar window (ts[i-1], ts[i]] (see _funding_into_bars). A
      position held into bar i pays/earns ``-position * funding_into[i]`` of
      notional (longs pay when funding_rate > 0). Pass None to disable (spot, or
      when no funding rows are available).

    Returns per-bar returns net of cost (and net of funding when provided).
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
            # Perp funding carry over the (i, i+1] window: a long pays funding
            # when funding_rate > 0, a short earns it (and vice-versa).
            if funding_into is not None:
                pnl[i + 1] -= position * float(funding_into[i + 1])

    return pnl


# ──────────────────── Perp funding carry ────────────────────

def _funding_symbol(instrument: str) -> str:
    """Map an OKX instrument id to the Binance funding-history symbol.

    'BTC-USDT-SWAP' → 'BTCUSDT'. The market_data.binance_funding_history table
    currently only carries BTCUSDT; other bases return a symbol with no rows
    (handled gracefully by the caller).
    """
    base = instrument.split("-")[0]
    quote = instrument.split("-")[1] if "-" in instrument else "USDT"
    return f"{base}{quote}"


async def _fetch_funding(instrument: str) -> list[tuple]:
    """Fetch (funding_time, funding_rate) rows for the instrument's perp.

    Returns [] when no rows exist (caller logs a warning and skips funding).
    """
    sym = _funding_symbol(instrument)
    conn = await asyncpg.connect(DB_DSN)
    try:
        rows = await conn.fetch(
            """SELECT funding_time, funding_rate::float
               FROM market_data.binance_funding_history
               WHERE symbol=$1
               ORDER BY funding_time""",
            sym,
        )
    finally:
        await conn.close()
    return [(r[0], float(r[1])) for r in rows]


def _funding_into_bars(ts_bars, funding_rows: list[tuple]) -> np.ndarray:
    """Aggregate funding rates onto the bar grid.

    Returns an array ``out`` of len(ts_bars) where ``out[i]`` is the sum of all
    funding_rate values whose funding_time falls in the bar window
    (ts_bars[i-1], ts_bars[i]].  ``out[0]`` is 0 (no preceding bar). Robust to an
    empty funding list (returns all-zeros).
    """
    n = len(ts_bars)
    out = np.zeros(n)
    if n == 0 or not funding_rows:
        return out
    ft = np.array([r[0].timestamp() for r in funding_rows], dtype=float)
    fr = np.array([r[1] for r in funding_rows], dtype=float)
    order = np.argsort(ft)
    ft, fr = ft[order], fr[order]
    csum = np.concatenate([[0.0], np.cumsum(fr)])
    bts = np.array([t.timestamp() for t in ts_bars], dtype=float)
    for i in range(1, n):
        a = int(np.searchsorted(ft, bts[i - 1], side="right"))
        b = int(np.searchsorted(ft, bts[i], side="right"))
        out[i] = csum[b] - csum[a]
    return out


# ──────────────────── Blocked walk-forward gate (sequential, embargoed —
#                      NOT combinatorial, NOT purged CPCV) ────────────────────

def _deflated_sharpe_real(
    pnl: np.ndarray,
    fold_sharpes: list[float],
    n_trials: int,
    periods_per_year: int,
) -> float:
    """REAL Deflated Sharpe Ratio (Bailey & López de Prado 2016). NON-gating.

    DSR = Φ( (SR̂ − SR*) · √(T−1) / √(1 − γ3·SR̂ + (γ4−1)/4·SR̂²) )

    where SR̂ is the per-observation Sharpe of the strategy, γ3/γ4 are the skew
    and (non-excess) kurtosis of the per-bar returns, T is the sample length, and
    SR* = √Var({SR_n}) · E[max of N standard normals] is the expected-max
    benchmark Sharpe under multiple testing (per-observation units).

    Returns a probability in [0,1]; NaN when undefined (T<3, zero variance, …).
    This is reported for analysis only and does NOT affect the verdict.
    """
    from scipy import stats

    r = np.asarray(pnl, dtype=float)
    T = len(r)
    sd = float(np.std(r))
    if T < 3 or sd < 1e-12:
        return float("nan")
    sr_obs = float(np.mean(r)) / sd                  # per-observation Sharpe
    skew = float(stats.skew(r))
    kurt = float(stats.kurtosis(r, fisher=False))    # non-excess (normal = 3)

    # Benchmark SR* in per-observation units: dispersion of trial Sharpes
    # (de-annualised) times the expected max of N standard normals.
    ann = math.sqrt(periods_per_year)
    sr_var = float(np.var(np.asarray(fold_sharpes) / ann)) if len(fold_sharpes) > 1 else 0.0
    sr_star = math.sqrt(sr_var) * _dsr_threshold(max(1, n_trials))

    denom = 1.0 - skew * sr_obs + (kurt - 1.0) / 4.0 * sr_obs ** 2
    if denom <= 0:
        return float("nan")
    z = (sr_obs - sr_star) * math.sqrt(T - 1) / math.sqrt(denom)
    return float(stats.norm.cdf(z))


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
    """Sequential blocked walk-forward gate with an embargo gap.

    NOTE ON LABELS: this is plain blocked CV, NOT Combinatorial Purged CV (folds
    are sequential, non-overlapping, separated by an embargo; there is no
    combinatorial recombination and no per-label purging). The two gating numbers
    are heuristics, not their textbook namesakes — the dict keys are kept for
    backward compatibility but the values are:
      - "deflated_sharpe" = mean_oos − std_oos  (mean-minus-dispersion heuristic;
        NOT the skew/kurtosis-adjusted Deflated Sharpe — that is reported
        separately as "deflated_sharpe_real").
      - "pbo"             = freq(IS_sharpe > OOS_sharpe) across folds (an IS>OOS
        frequency; NOT the CSCV logit-rank Probability of Backtest Overfitting).

    Per fold: split into first 2/3 (IS) + last 1/3 (OOS); collect IS/OOS Sharpe.
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
    # "pbo" key = frequency of IS Sharpe > OOS Sharpe (heuristic, not CSCV PBO).
    pbo      = pbo_count / len(oos_sharpes)

    # "deflated_sharpe" key = mean_oos − std_oos: a mean-minus-dispersion
    # heuristic that penalises high cross-fold variance. This is NOT the
    # skew/kurtosis-adjusted Deflated Sharpe of Bailey & López de Prado — see
    # _deflated_sharpe_real() for the real figure (reported, non-gating).
    n_f       = len(oos_sharpes)
    oos_std   = float(np.std(oos_sharpes)) if n_f > 1 else 0.0
    dsr       = mean_oos - oos_std

    fail_reasons = []
    if dsr <= 0:
        fail_reasons.append(f"mean_oos−std_oos={dsr:.3f} ≤ 0")
    if pbo >= pbo_threshold:
        fail_reasons.append(f"IS>OOS freq={pbo:.2f} ≥ {pbo_threshold}")

    status = "PASS" if not fail_reasons else "FAIL"

    return {
        "oos_sharpes":      oos_sharpes,
        "is_sharpes":       is_sharpes,
        "mean_oos_sharpe":  mean_oos,
        "pbo":              pbo,              # = is_gt_oos_freq (kept for consumers)
        "is_gt_oos_freq":   pbo,             # honest alias
        "deflated_sharpe":  dsr,              # = mean_minus_std_oos (kept for consumers)
        "mean_minus_std_oos": dsr,           # honest alias
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
        print(f"Trial  : #{trials_before + 1}  (expected-max-of-N benchmark Sharpe: {dsr_threshold:.3f})")
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
            ohlcv = {k: raw[k] for k in ("ts", "open", "high", "low", "close", "volume")}

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

        # Perp funding carry: default ON for SWAP instruments, OFF for spot.
        # Overridable via cfg['funding']['enabled'].
        funding_into = None
        funding_default = inst.upper().endswith("SWAP")
        funding_enabled = bool(cfg.get("funding", {}).get("enabled", funding_default))
        if funding_enabled:
            ts_bars = ohlcv.get("ts")
            if ts_bars is None:
                print(f"  [WARN] funding requested but no bar timestamps available — skipping funding")
            else:
                frows = await _fetch_funding(inst)
                if not frows:
                    print(f"  [WARN] no funding rows for {inst} "
                          f"(symbol {_funding_symbol(inst)}) — funding carry = 0")
                else:
                    funding_into = _funding_into_bars(ts_bars, frows)
                    if verbose:
                        print(f"  Funding: {len(frows)} rows, "
                              f"Σrate over span = {float(np.sum(funding_into)):.5f}")

        # P&L — direction from config
        closes    = ohlcv["close"]
        direction = cfg.get("signal_logic", {}).get("direction", "both")
        pnl       = _signals_to_pnl(signals, closes, cost_bps, direction=direction,
                                    funding_into=funding_into)

        gross_sr = _sharpe(pnl, ppy)
        if verbose:
            print(f"  Gross Sharpe (net of cost{' + funding' if funding_into is not None else ''}): {gross_sr:.3f}")

        # Blocked walk-forward gate (sequential, embargoed)
        gate = _walk_forward_gate(pnl, n_splits, embargo_bars, ppy, pbo_threshold)
        gate["gross_sharpe"] = gross_sr

        # REAL Deflated Sharpe (Bailey & López de Prado) — reported, NON-gating.
        gate["deflated_sharpe_real"] = _deflated_sharpe_real(
            pnl, gate.get("oos_sharpes", []), trials_before + 1, ppy
        )

        # Apply expected-max-of-N benchmark to the mean−std heuristic (this is the
        # legit multiple-testing correction; key name kept for compatibility).
        adjusted_dsr = gate["deflated_sharpe"] - dsr_threshold
        gate["adjusted_dsr"]   = adjusted_dsr
        gate["dsr_threshold"]  = dsr_threshold
        gate["trial_n"]        = trials_before + 1

        # Re-check PASS against the trial-count-adjusted benchmark
        if not math.isnan(gate["deflated_sharpe"]) and gate["deflated_sharpe"] > 0 and adjusted_dsr <= 0:
            gate["fail_reasons"].append(f"mean_oos−std_oos={gate['deflated_sharpe']:.3f} > 0 but adjusted={adjusted_dsr:.3f} ≤ 0 (N={trials_before+1} trials)")
            gate["status"] = "FAIL"

        if verbose:
            real_dsr = gate["deflated_sharpe_real"]
            print(f"  OOS Sharpes: {[f'{s:.3f}' for s in gate['oos_sharpes']]}")
            print(f"  Mean OOS: {gate['mean_oos_sharpe']:.3f}  "
                  f"mean−std_oos(='dsr'): {gate['deflated_sharpe']:.3f}  "
                  f"adj: {adjusted_dsr:.3f}  "
                  f"IS>OOS freq(='pbo'): {gate['pbo']:.2f}")
            print(f"  Real Deflated Sharpe (diagnostic, non-gating): "
                  f"{real_dsr:.3f}" if not math.isnan(real_dsr) else
                  "  Real Deflated Sharpe (diagnostic, non-gating): n/a")
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
                "dsr":          v.get("deflated_sharpe"),        # mean_oos − std_oos heuristic
                "pbo":          v.get("pbo"),                    # IS>OOS frequency heuristic
                "mean_oos":     v.get("mean_oos_sharpe"),
                "gross_sharpe": v.get("gross_sharpe"),
                "deflated_sharpe_real": v.get("deflated_sharpe_real"),  # real DSR, non-gating
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
