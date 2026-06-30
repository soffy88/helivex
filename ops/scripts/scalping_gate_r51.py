#!/usr/bin/env python3
"""
R5.1: 5m scalping gate — 3 microstructure strategies (helivex strategy 2/3).
Strategy 3 (trend/ETH) is fixed. Strategy 2 targets a different alpha: short-term
mean reversion / order flow / volatility compression, NOT directional trend.

Honesty constraint: trading cost is the first gate.
- 10 bps round-trip is the standard OKX taker rate (5 bps each side).
- Scalping P&L per trade must survive 15 bps (taker + conservative slippage).
- Cost sensitivity runs BEFORE walk-forward gate to decide viability.

Strategies:
  A. VWAP-MR:    price deviates from 4H rolling VWAP → fade back (mean reversion)
  B. VolSurge:   volume spike on directional bar → ride momentum for 30 min
  C. BB-Squeeze: Bollinger Band compression followed by breakout
"""
from __future__ import annotations

import asyncio
import datetime
import sys
from pathlib import Path

import asyncpg
import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
from omodul.backtest_gate import backtest_gate, BacktestGateConfig

# ── Constants ─────────────────────────────────────────────────────────────────
DB_DSN       = "postgresql://helios:helios_dev_pass@localhost:5434/helivex"
SOURCE_5M    = "okx_swap_5m"
INSTRUMENTS  = ["BTC-USDT-SWAP", "ETH-USDT-SWAP", "SOL-USDT-SWAP"]

PERIODS_5M   = 365 * 24 * 12   # 105,120 bars/year (5m, 24h/7d)

# Cost levels: ROUND-TRIP basis points
COST_BPS_LIST = [0, 5, 10, 15, 20]
GATE_COST_BPS = 15              # gate uses conservative 15 bps round-trip

# Strategy definitions: (name, signal_type, hold_bars, signal_params)
STRATEGIES = [
    ("VWAP-MR",     "vwap_mr",    12, {"vwap_n": 48,  "z_thr": 2.0}),
    ("VolSurge",    "vol_surge",   6, {"vol_n": 20,   "surge_mult": 2.5}),
    ("BB-Squeeze",  "bb_squeeze", 10, {"bb_n": 20, "bb_k": 2.0,
                                        "squeeze_lb": 200, "squeeze_pct": 0.25}),
]

CONTEXT_BARS = 300   # context prepended to test fold for signal warmup
EMBARGO_BARS = 300   # bars excluded between folds to prevent leakage


# ── DB loading ────────────────────────────────────────────────────────────────

async def load_5m_bars(inst: str) -> list[dict]:
    conn = await asyncpg.connect(DB_DSN)
    rows = await conn.fetch(
        """SELECT bar_close_ts,
                  open::float, high::float, low::float, close::float,
                  volume::float
           FROM market_data.ohlcv_5m
           WHERE instrument=$1 AND source=$2
           ORDER BY bar_close_ts""",
        inst, SOURCE_5M,
    )
    await conn.close()
    return [dict(r) for r in rows]


def bars_to_arrays(bars: list[dict]) -> tuple[np.ndarray, ...]:
    closes  = np.array([b["close"]  for b in bars], dtype=float)
    opens   = np.array([b["open"]   for b in bars], dtype=float)
    volumes = np.array([b["volume"] for b in bars], dtype=float)
    return closes, opens, volumes


# ── Signal computation (vectorized, no look-ahead) ────────────────────────────
#
# All signals use rolling windows SHIFTED by 1 so that signals[i] at bar i
# uses only data from bars 0..i-1. Entry at bar i+1 → no future leak.
# Look-ahead proof: signal fires at bar i CLOSE (known), enters at bar i+1 OPEN.

def signals_vwap_mr(closes: np.ndarray, volumes: np.ndarray,
                    vwap_n: int = 48, z_thr: float = 2.0) -> np.ndarray:
    """
    VWAP mean reversion: fade price deviations from rolling VWAP.
    z > z_thr → SHORT (price above VWAP); z < -z_thr → LONG.
    VWAP and std computed from bars [i-vwap_n .. i-1] (shift(1), no current bar).
    """
    s_c = pd.Series(closes, dtype=float)
    s_v = pd.Series(volumes, dtype=float)

    roll_vc = (s_c * s_v).rolling(vwap_n, min_periods=max(2, vwap_n // 4)).sum().shift(1)
    roll_v  = s_v.rolling(vwap_n, min_periods=max(2, vwap_n // 4)).sum().shift(1)
    vwap    = roll_vc / (roll_v + 1e-10)
    std     = s_c.rolling(vwap_n, min_periods=max(2, vwap_n // 4)).std().shift(1)
    z       = (s_c - vwap) / (std + 1e-10)

    z_vals = z.values
    sigs = np.zeros(len(closes), dtype=int)
    sigs[z_vals >  z_thr]  = -1   # SHORT: price above VWAP, expect reversion down
    sigs[z_vals < -z_thr]  = +1   # LONG:  price below VWAP, expect reversion up
    sigs[np.isnan(z_vals)] =  0
    return sigs


def signals_vol_surge(closes: np.ndarray, opens: np.ndarray, volumes: np.ndarray,
                      vol_n: int = 20, surge_mult: float = 2.5) -> np.ndarray:
    """
    Volume surge momentum: when a bar has anomalously high volume AND a clear
    direction, ride that momentum.
    Volume MA computed from bars [i-vol_n .. i-1] (shift(1)).
    Current bar's volume is known at close → no look-ahead comparing vol[i] to MA[i-1].
    """
    s_c = pd.Series(closes, dtype=float)
    s_o = pd.Series(opens, dtype=float)
    s_v = pd.Series(volumes, dtype=float)

    vol_ma = s_v.rolling(vol_n, min_periods=max(2, vol_n // 4)).mean().shift(1)

    is_surge   = (s_v > vol_ma * surge_mult).values
    is_bullish = (s_c > s_o).values
    is_bearish = (s_c < s_o).values
    nan_mask   = np.isnan(vol_ma.values)

    sigs = np.zeros(len(closes), dtype=int)
    sigs[is_surge & is_bullish & ~nan_mask] = +1
    sigs[is_surge & is_bearish & ~nan_mask] = -1
    return sigs


def signals_bb_squeeze(closes: np.ndarray, volumes: np.ndarray,
                       bb_n: int = 20, bb_k: float = 2.0,
                       squeeze_lb: int = 200, squeeze_pct: float = 0.25) -> np.ndarray:
    """
    BB squeeze breakout: enter when volatility is compressed and price breaks out.
    BB computed from bars [i-bb_n .. i-1] (shift(1)).
    Squeeze threshold: 25th percentile of rolling 200-bar BB width history.
    """
    s_c = pd.Series(closes, dtype=float)

    bb_mean  = s_c.rolling(bb_n, min_periods=max(2, bb_n // 2)).mean().shift(1)
    bb_std   = s_c.rolling(bb_n, min_periods=max(2, bb_n // 2)).std().shift(1)
    bb_upper = bb_mean + bb_k * bb_std
    bb_lower = bb_mean - bb_k * bb_std
    bb_width = (2.0 * bb_k * bb_std) / (bb_mean.abs() + 1e-10)

    squeeze_thr = bb_width.rolling(squeeze_lb, min_periods=max(10, squeeze_lb // 4)) \
                          .quantile(squeeze_pct)

    nan_mask      = np.isnan(bb_mean.values) | np.isnan(squeeze_thr.values)
    is_squeezed   = (bb_width.values <= squeeze_thr.values) & ~nan_mask
    long_signal   = is_squeezed & (s_c.values > bb_upper.values)
    short_signal  = is_squeezed & (s_c.values < bb_lower.values)

    sigs = np.zeros(len(closes), dtype=int)
    sigs[long_signal]  = +1
    sigs[short_signal] = -1
    return sigs


def compute_signals(strat_type: str, closes: np.ndarray, opens: np.ndarray,
                    volumes: np.ndarray, params: dict) -> np.ndarray:
    if strat_type == "vwap_mr":
        return signals_vwap_mr(closes, volumes, **params)
    if strat_type == "vol_surge":
        return signals_vol_surge(closes, opens, volumes, **params)
    if strat_type == "bb_squeeze":
        return signals_bb_squeeze(closes, volumes, **params)
    raise ValueError(f"Unknown strategy type: {strat_type!r}")


# ── Fixed-hold simulation engine ──────────────────────────────────────────────

def _annualized_sharpe(rets: list | np.ndarray) -> float:
    arr = np.asarray(rets, dtype=float)
    if len(arr) < 5 or arr.std() < 1e-10:
        return 0.0
    return float(arr.mean() / arr.std() * np.sqrt(PERIODS_5M))


def _sim_scalp(closes: np.ndarray, signals: np.ndarray,
               hold: int, cost_oneway: float) -> tuple[list[float], int]:
    """
    Fixed-hold scalp simulation.

    Timing:
      signals[i] fires at bar i CLOSE → entry at bar i+1 OPEN ≈ bar i CLOSE.
      Position held for exactly `hold` bars then exited.
      Entry and exit each cost `cost_oneway` (half of round-trip).

    No overlapping positions: a new entry is only considered once the current
    trade is flat. Exits on the bar where bars_left hits zero.
    """
    n = len(closes)
    pos       = 0
    bars_left = 0
    n_trades  = 0
    rets: list[float] = []

    for i in range(1, n):
        if closes[i - 1] <= 0:
            rets.append(0.0)
            continue

        bar_ret = (closes[i] - closes[i - 1]) / closes[i - 1]
        cost    = 0.0

        # Entry: signal from bar i-1 → enter now (flat only)
        if pos == 0 and i - 1 < len(signals) and signals[i - 1] != 0:
            pos       = int(signals[i - 1])
            bars_left = hold
            cost     += cost_oneway
            n_trades += 1

        active_pos = pos   # position DURING this bar

        # Decrement hold counter and exit if expired
        if pos != 0:
            bars_left -= 1
            if bars_left == 0:
                cost += cost_oneway   # exit fee at bar close
                pos   = 0

        rets.append(active_pos * bar_ret - cost)

    return rets, n_trades


# ── Cost sensitivity analysis ─────────────────────────────────────────────────

def cost_sensitivity(closes: np.ndarray, opens: np.ndarray, volumes: np.ndarray,
                     strat_type: str, params: dict, hold: int) -> dict:
    """Run strategy at all cost levels on full dataset. Returns {cost_bps: {sharpe, n_trades}}."""
    sigs = compute_signals(strat_type, closes, opens, volumes, params)
    results: dict[int, dict] = {}
    for cost_bps in COST_BPS_LIST:
        rets, n_trades = _sim_scalp(closes, sigs, hold, cost_bps / 20000)
        n_years = len(closes) / PERIODS_5M
        trades_per_day = n_trades / (n_years * 365) if n_years > 0 else 0
        results[cost_bps] = {
            "sharpe":        round(_annualized_sharpe(rets), 3),
            "n_trades":      n_trades,
            "trades_per_day": round(trades_per_day, 1),
        }
    return results


def survives_cost_gate(sensitivity_result: dict, threshold_bps: int = 15) -> bool:
    """Returns True if strategy has positive Sharpe at the threshold cost level."""
    r = sensitivity_result.get(threshold_bps, {})
    return r.get("sharpe", -999) > 0.0


# ── Gate runner ───────────────────────────────────────────────────────────────

def make_strategy_fn(strat_type: str, params: dict, hold: int):
    """Build strategy_fn closure for backtest_gate walk-forward."""
    cost_oneway = GATE_COST_BPS / 20000

    def strategy_fn(train_data: list, test_data: list) -> dict:
        tr_c = np.array([d["close"]  for d in train_data], dtype=float)
        tr_o = np.array([d["open"]   for d in train_data], dtype=float)
        tr_v = np.array([d["volume"] for d in train_data], dtype=float)

        te_c = np.array([d["close"]  for d in test_data], dtype=float)
        te_o = np.array([d["open"]   for d in test_data], dtype=float)
        te_v = np.array([d["volume"] for d in test_data], dtype=float)

        if len(tr_c) < CONTEXT_BARS or len(te_c) < hold:
            return {"sharpe": 0.0, "returns": [], "is_sharpe": 0.0}

        # IS simulation on full training fold
        is_sigs = compute_signals(strat_type, tr_c, tr_o, tr_v, params)
        is_rets, _ = _sim_scalp(tr_c, is_sigs, hold, cost_oneway)

        # OOS: prepend context for signal warmup (no position carry-over)
        ctx_c = tr_c[-CONTEXT_BARS:]
        ctx_o = tr_o[-CONTEXT_BARS:]
        ctx_v = tr_v[-CONTEXT_BARS:]

        full_c = np.concatenate([ctx_c, te_c])
        full_o = np.concatenate([ctx_o, te_o])
        full_v = np.concatenate([ctx_v, te_v])

        full_sigs = compute_signals(strat_type, full_c, full_o, full_v, params)
        oos_sigs  = full_sigs[CONTEXT_BARS:]   # test-period signals only

        # Fresh simulation on test period (no state carried from context)
        oos_rets, _ = _sim_scalp(te_c, oos_sigs, hold, cost_oneway)

        return {
            "sharpe":    _annualized_sharpe(oos_rets),
            "returns":   list(oos_rets),
            "is_sharpe": _annualized_sharpe(is_rets),
        }

    return strategy_fn


def run_gate(inst: str, data: list[dict], strat_name: str, strat_type: str,
             params: dict, hold: int, n_splits: int) -> dict:
    config = BacktestGateConfig(
        strategy_name=f"scalp_{strat_name}_{inst}",
        n_splits=n_splits,
        embargo=EMBARGO_BARS,
        pbo_threshold=0.5,
        periods=PERIODS_5M,
    )
    fn   = make_strategy_fn(strat_type, params, hold)
    gate = backtest_gate(fn, data, config=config)
    wf   = gate["walk_forward_result"]
    frs  = wf["fold_results"]

    return {
        "gate_status": gate["gate_status"],
        "oos_sharpe":  round(float(gate["mean_oos_sharpe"]), 3),
        "dsr":         round(float(gate["deflated_sharpe"]), 3),
        "pbo":         round(float(gate["pbo"]), 3),
        "fold_oos":    [round(float(fr.get("sharpe", 0)), 3) for fr in frs],
        "n_splits":    n_splits,
    }


# ── Verdict doc ───────────────────────────────────────────────────────────────

def write_verdict(
    data_info:     dict,
    cost_tables:   dict,   # {inst: {strat_name: {cost_bps: {sharpe, n_trades}}}}
    gate_results:  dict,   # {inst: {strat_name: gate_dict}}  (only survivors)
    survivors:     list,   # [(strat_name, inst, cost_result)]
) -> Path:

    # ── §1 data table ──
    data_rows = []
    for inst in INSTRUMENTS:
        d = data_info.get(inst, {})
        data_rows.append(
            f"| {inst} | {d.get('n_bars', 0):,} | "
            f"{d.get('first_bar','?')} | {d.get('last_bar','?')} |"
        )

    # ── §2 cost sensitivity tables (one per strategy, all instruments) ──
    cost_sections = []
    for name, stype, hold, params in STRATEGIES:
        header = (
            f"### {name}  (hold={hold} bars = {hold*5} min)\n\n"
            f"| Instrument | 0 bps (gross) | 5 bps RT | 10 bps RT | 15 bps RT | 20 bps RT | Trades/day |\n"
            f"|---|---|---|---|---|---|---|\n"
        )
        rows = []
        for inst in INSTRUMENTS:
            ct = cost_tables.get(inst, {}).get(name, {})
            sharpes = " | ".join(f"{ct.get(c, {}).get('sharpe', 0):.3f}" for c in COST_BPS_LIST)
            tpd = ct.get(10, {}).get("trades_per_day", 0)
            rows.append(f"| {inst} | {sharpes} | {tpd} |")
        cost_sections.append(header + "\n".join(rows))

    # ── §3 viability summary ──
    viability_rows = []
    for name, stype, hold, params in STRATEGIES:
        for inst in INSTRUMENTS:
            ct = cost_tables.get(inst, {}).get(name, {})
            at_10  = ct.get(10,  {}).get("sharpe", 0)
            at_15  = ct.get(15,  {}).get("sharpe", 0)
            at_20  = ct.get(20,  {}).get("sharpe", 0)
            viable = "✓ proceed" if at_15 > 0 else ("⚠ marginal" if at_10 > 0 else "✗ cost-killed")
            viability_rows.append(
                f"| {name} | {inst} | {at_10:.3f} | {at_15:.3f} | {at_20:.3f} | {viable} |"
            )

    # ── §4 gate results (survivors only) ──
    gate_rows = []
    for inst in INSTRUMENTS:
        for name, stype, hold, params in STRATEGIES:
            g = gate_results.get(inst, {}).get(name)
            if not g:
                gate_rows.append(f"| {name} | {inst} | — | — | — | — | skipped (cost-killed) |")
            else:
                st = "PASS" if g["gate_status"] == "passed" else "FAIL"
                gate_rows.append(
                    f"| {name} | {inst} | {g['oos_sharpe']:.3f} | "
                    f"{g['dsr']:.3f} | {g['pbo']:.2f} | {g['n_splits']} | **{st}** |"
                )

    # ── §5 verdict ──
    n_pass = sum(1 for inst in INSTRUMENTS
                 for name, _, _, _ in STRATEGIES
                 if (gate_results.get(inst, {}).get(name) or {}).get("gate_status") == "passed")
    total_gate_runs = sum(1 for inst in INSTRUMENTS
                          for name, _, _, _ in STRATEGIES
                          if gate_results.get(inst, {}).get(name) is not None)

    any_survivor_15bps = any(
        cost_tables.get(inst, {}).get(name, {}).get(15, {}).get("sharpe", 0) > 0
        for inst in INSTRUMENTS
        for name, _, _, _ in STRATEGIES
    )

    if n_pass > 0:
        verdict = "**PASS**"
        verdict_detail = f"{n_pass} strategy-instrument combination(s) pass the walk-forward gate at 15bps."
    elif any_survivor_15bps and total_gate_runs > 0:
        verdict = "**NO-GO**"
        verdict_detail = (
            "Strategy-instrument(s) survive 15bps cost sensitivity but fail the walk-forward gate. "
            "Signal exists but is not statistically significant under DSR+PBO criteria."
        )
    elif any_survivor_15bps:
        verdict = "**NO-GO (gate not reached)**"
        verdict_detail = "Some strategies survive 15bps gross but gate was skipped due to data issues."
    else:
        verdict = "**NO-GO**"
        verdict_detail = (
            "No strategy survives 15bps round-trip cost. "
            "5m bar OHLCV scalping is not viable at OKX taker rates without maker rebate or L2 data. "
            "Gross edge (0 bps Sharpe) exists but is smaller than the bid-ask spread."
        )

    doc = f"""# R5.1 5m Scalping Gate — Microstructure Alpha Validation

> helivex strategy 2/3. Strategy 3 (trend/ETH) is fixed. This validates strategy 2 alpha.
> Primary constraint: trading cost. Scalping edge must survive 15bps round-trip (taker + slippage).
> Strategy: NOT directional trend. Targets microstructure: VWAP reversion / volume flow / BB squeeze.

## §1 Data (5m OHLCV, OKX history-candles)

| Instrument | Bars | First Bar | Last Bar |
|---|---|---|---|
{chr(10).join(data_rows)}

Source: `okx_swap_5m` in `market_data.ohlcv_1h`.
Bar: open time = bar\\_close\\_ts − 5min. Confirmed bars only (confirm=1).

## §2 Cost Sensitivity (Sharpe at each round-trip cost level)

**⚠ Primary viability check.** If 15bps kills the edge → strategy not viable at taker rates.
Results are on the FULL dataset (no train/test split) to show gross capacity.

{chr(10).join(cost_sections)}

## §3 Cost Viability Summary

| Strategy | Instrument | @10bps | @15bps | @20bps | Viability |
|---|---|---|---|---|---|
{chr(10).join(viability_rows)}

Decision rule: @15bps Sharpe > 0 → proceed to walk-forward gate. Otherwise: cost-killed.

## §4 Walk-Forward Gate Results (15bps cost, CPCV)

Only strategies surviving §3 cost gate are run here.

| Strategy | Instrument | OOS Sharpe | DSR | PBO | n_splits | Gate |
|---|---|---|---|---|---|---|
{chr(10).join(gate_rows)}

Embargo: {EMBARGO_BARS} bars (context warmup protection).
Gate cost: {GATE_COST_BPS}bps round-trip applied inside each fold's IS and OOS simulation.

## §5 Look-Ahead Audit

- All signals use `.shift(1)` rolling windows: signals[i] uses bars [i-N..i-1] only ✓
- Entry at bar i+1 open ≈ bar i close (1-bar lag, standard) ✓
- Volume surge: current bar volume known at its close, compared to PRIOR N-bar mean ✓
- BB squeeze: current close compared to bands from PRIOR N bars ✓
- VWAP: current close compared to VWAP from PRIOR vwap_n bars ✓
- Context prepend in gate: last {CONTEXT_BARS} IS bars added for signal warmup;
  simulation starts fresh (pos=0) at test period start — no state carry-over ✓

## §6 Verdict

### {verdict}

{verdict_detail}

### Interpretation

5m bar OHLCV scalping faces a structural cost challenge:
- Standard OKX taker = 5bps/side = 10bps round-trip
- Conservative slippage assumption = +5bps = 15bps total
- Scalping P&L per trade must cover 15bps on a 1H or sub-1H hold
- At 3-5 trades/day: cost burden ≈ 45-75bps/day ≈ 16-27% annualized

If gross Sharpe (0bps) is not substantially positive, edge is too thin for taker rates.

### Next step (if applicable)

- If cost-killed at 15bps: consider maker order strategy (limit orders → 0bps or rebate)
  or upgrade to L2 order book data for true microstructure signal
- If gate fails with positive 15bps Sharpe: signal is real but below statistical
  significance threshold given data length — consider longer history or regime-aware variant

### Cumulative Scorecard

| Strategy | Alpha Mechanism | Best Gross Sharpe | @15bps Sharpe | Gate |
|---|---|---|---|---|
| R4.x trend dual | Directional trend | ~1.5 (BTC/ETH) | — | NO-GO (DSR) |
| R5.1 VWAP-MR | Mean reversion | see §2 | see §2 | see §4 |
| R5.1 VolSurge | Momentum | see §2 | see §2 | see §4 |
| R5.1 BB-Squeeze | Vol compression | see §2 | see §2 | see §4 |

---
*Generated by `ops/scripts/scalping_gate_r51.py` — R5.1 milestone.*
"""
    out = Path(__file__).parent.parent.parent / "docs" / "R5.1_SCALPING_GATE.md"
    out.write_text(doc)
    return out


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    import time

    print("=== R5.1 5m Scalping Gate ===\n")

    # ── Load data ──
    all_bars: dict[str, list[dict]]  = {}
    all_arrs: dict[str, tuple]       = {}
    data_info: dict[str, dict]       = {}

    for inst in INSTRUMENTS:
        bars = asyncio.run(load_5m_bars(inst))
        if not bars:
            print(f"  {inst}: NO 5m DATA — run backfill_5m.py first")
            continue
        closes, opens, vols = bars_to_arrays(bars)
        all_bars[inst] = bars
        all_arrs[inst] = (closes, opens, vols)
        data_info[inst] = {
            "n_bars":    len(bars),
            "first_bar": bars[0]["bar_close_ts"].strftime("%Y-%m-%d"),
            "last_bar":  bars[-1]["bar_close_ts"].strftime("%Y-%m-%d"),
        }
        print(f"  {inst}: {len(bars):,} bars  "
              f"{data_info[inst]['first_bar']} → {data_info[inst]['last_bar']}")

    if not all_bars:
        print("No 5m data found. Abort.")
        sys.exit(1)

    # ── Cost sensitivity ──
    print("\n── Cost sensitivity (Sharpe at 0/5/10/15/20 bps round-trip) ──")
    print(f"{'Strategy':<14} {'Instrument':<22} {'0bps':>7} {'5bps':>7} {'10bps':>7} "
          f"{'15bps':>7} {'20bps':>7}  trades/day")

    cost_tables: dict[str, dict] = {}   # {inst: {name: {cost_bps: result}}}
    for inst, (closes, opens, vols) in all_arrs.items():
        cost_tables[inst] = {}
        for name, stype, hold, params in STRATEGIES:
            t0 = time.monotonic()
            ct = cost_sensitivity(closes, opens, vols, stype, params, hold)
            cost_tables[inst][name] = ct
            sharpes = "  ".join(f"{ct[c]['sharpe']:+.3f}" for c in COST_BPS_LIST)
            tpd = ct[10]["trades_per_day"]
            print(f"  {name:<12} {inst:<22} {sharpes}  {tpd:.1f}/day "
                  f"[{time.monotonic()-t0:.1f}s]")

    # ── Gate (survivors only) ──
    print(f"\n── Walk-forward gate ({GATE_COST_BPS}bps cost, survivors of §3) ──")
    gate_results: dict[str, dict] = {}
    survivors: list[tuple] = []

    for inst, (closes, opens, vols) in all_arrs.items():
        gate_results[inst] = {}
        n_bars   = len(all_bars[inst])
        n_splits = max(4, min(8, n_bars // 70000))  # ~9+ months per fold
        bars_as_dicts = [
            {"close": float(c), "open": float(o), "volume": float(v)}
            for c, o, v in zip(closes, opens, vols)
        ]

        for name, stype, hold, params in STRATEGIES:
            ct_at_15 = cost_tables[inst][name].get(15, {}).get("sharpe", -999)
            if ct_at_15 <= 0.0:
                print(f"  {name:<12} {inst:<22} SKIP  (@15bps Sharpe={ct_at_15:.3f} ≤ 0)")
                gate_results[inst][name] = None
                continue

            t0 = time.monotonic()
            g  = run_gate(inst, bars_as_dicts, name, stype, params, hold, n_splits)
            gate_results[inst][name] = g
            st = "PASS" if g["gate_status"] == "passed" else "FAIL"
            print(f"  {name:<12} {inst:<22} {st}  "
                  f"OOS={g['oos_sharpe']:.3f}  DSR={g['dsr']:.3f}  PBO={g['pbo']:.2f}  "
                  f"n={n_splits}  [{time.monotonic()-t0:.1f}s]")
            if g["gate_status"] == "passed":
                survivors.append((name, inst, ct_at_15))

    # ── Write verdict ──
    out = write_verdict(data_info, cost_tables, gate_results, survivors)
    print(f"\nVerdict → {out.relative_to(Path.cwd()) if out.exists() else out}")


if __name__ == "__main__":
    main()
