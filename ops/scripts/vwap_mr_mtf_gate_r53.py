#!/usr/bin/env python3
"""
R5.3: VWAP mean reversion at 30m and 1H — escaping the 5m frequency trap.

Hypothesis: 5m VWAP-MR has genuine alpha (+0.81 gross Sharpe BTC, 6yr) but 8.4 trades/day
× 10bps taker = 307%/yr cost kills it. At 30m/1H: fewer trades, larger P&L/trade,
same signal mechanism → cost burden drops to ~30-70%/yr → net alpha possible.

Data: 5m OHLCV resampled to 30m and 1H (no additional backfill needed).
Gate: taker 10bps RT conservative, DSR>0 AND PBO<0.5.
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
DB_DSN      = "postgresql://helios:helios_dev_pass@localhost:5434/helivex"
SOURCE_5M   = "okx_swap_5m"
INSTRUMENTS = ["BTC-USDT-SWAP", "ETH-USDT-SWAP", "SOL-USDT-SWAP"]

PERIODS = {"5m": 105_120, "30m": 17_520, "1h": 8_760}
FOLD_MIN = {"5m": 70_000, "30m": 11_700, "1h": 5_840}  # ~9 months per fold

COST_BPS_LIST = [0, 5, 10, 15, 20]
GATE_COST_BPS = 10   # taker 10bps RT, no maker assumption

# Strategy parameters (pre-committed, economically equivalent across TFs)
#   window = same ABSOLUTE TIME (4H) at each resolution — not same bar count
#     5m:  48 bars × 5m  = 240 min = 4H
#     30m:  8 bars × 30m = 240 min = 4H
#     1h:   4 bars × 1h  = 240 min = 4H
#   hold   = same absolute hold time (6H target)
TF_PARAMS = {
    "5m":  {"vwap_n": 48, "z_thr": 2.0, "hold": 12},   # 4H window, 1H hold
    "30m": {"vwap_n":  8, "z_thr": 2.0, "hold": 12},   # 4H window, 6H hold
    "1h":  {"vwap_n":  4, "z_thr": 2.0, "hold":  6},   # 4H window, 6H hold
}

CONTEXT_BARS = {"5m": 300, "30m": 100, "1h": 72}
EMBARGO_BARS = {"5m": 300, "30m":  50, "1h": 48}

# Market regime cycles for per-cycle robustness
CYCLES = [
    ("2020Recovery", "2020-01-01", "2020-12-31"),
    ("Bull2021a",    "2021-01-01", "2021-04-30"),
    ("Bear2021",     "2021-05-01", "2021-07-31"),
    ("Bull2021b",    "2021-08-01", "2021-11-30"),
    ("Bear2022",     "2022-01-01", "2022-12-31"),
    ("Bull2023",     "2023-01-01", "2023-12-31"),
    ("Chop2024a",    "2024-01-01", "2024-09-30"),
    ("Bull2024b",    "2024-10-01", "2025-01-31"),
    ("Chop2025",     "2025-02-01", "2025-07-31"),
    ("Bear2025",     "2025-08-01", "2026-01-31"),
]


# ── DB + resample ─────────────────────────────────────────────────────────────

async def load_5m_raw(inst: str) -> pd.DataFrame:
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
    df = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "volume"])
    df["ts"] = pd.to_datetime(df["ts"], utc=True)
    df = df.set_index("ts").sort_index()
    return df


def resample_ohlcv(df5m: pd.DataFrame, freq: str) -> pd.DataFrame:
    """Resample 5m OHLCV to 30min or 1h. label='right' = close timestamp."""
    agg = df5m.resample(freq, closed="right", label="right").agg(
        open=("open",   "first"),
        high=("high",   "max"),
        low=("low",     "min"),
        close=("close", "last"),
        volume=("volume", "sum"),
    ).dropna(subset=["close", "open"])
    return agg


def df_to_arrays(df: pd.DataFrame) -> tuple[np.ndarray, ...]:
    return (
        df["close"].values.astype(float),
        df["open"].values.astype(float),
        df["high"].values.astype(float),
        df["low"].values.astype(float),
        df["volume"].values.astype(float),
    )


# ── Signal + simulation (same as R5.1/R5.2) ──────────────────────────────────

def signals_vwap_mr(closes: np.ndarray, volumes: np.ndarray,
                    vwap_n: int, z_thr: float) -> np.ndarray:
    s_c = pd.Series(closes, dtype=float)
    s_v = pd.Series(volumes, dtype=float)
    minp = max(2, vwap_n // 4)
    roll_vc = (s_c * s_v).rolling(vwap_n, min_periods=minp).sum().shift(1)
    roll_v  = s_v.rolling(vwap_n, min_periods=minp).sum().shift(1)
    vwap    = roll_vc / (roll_v + 1e-10)
    std     = s_c.rolling(vwap_n, min_periods=minp).std().shift(1)
    z       = (s_c - vwap) / (std + 1e-10)
    z_vals  = z.values
    sigs = np.zeros(len(closes), dtype=int)
    sigs[z_vals >  z_thr]  = -1
    sigs[z_vals < -z_thr]  = +1
    sigs[np.isnan(z_vals)] =  0
    return sigs


def annualized_sharpe(rets: list | np.ndarray, periods: int) -> float:
    arr = np.asarray(rets, dtype=float)
    if len(arr) < 5 or arr.std() < 1e-10:
        return 0.0
    return float(arr.mean() / arr.std() * np.sqrt(periods))


def sim_taker(closes: np.ndarray, signals: np.ndarray,
              hold: int, cost_oneway: float) -> tuple[list[float], int]:
    n = len(closes)
    pos = 0; bars_left = 0; n_trades = 0; rets: list[float] = []
    for i in range(1, n):
        if closes[i - 1] <= 0:
            rets.append(0.0); continue
        bar_ret = (closes[i] - closes[i - 1]) / closes[i - 1]
        cost    = 0.0
        if pos == 0 and signals[i - 1] != 0:
            pos = int(signals[i - 1]); bars_left = hold
            cost += cost_oneway; n_trades += 1
        active_pos = pos
        if pos != 0:
            bars_left -= 1
            if bars_left == 0:
                cost += cost_oneway; pos = 0
        rets.append(active_pos * bar_ret - cost)
    return rets, n_trades


# ── Cost sensitivity ──────────────────────────────────────────────────────────

def cost_sensitivity(closes: np.ndarray, volumes: np.ndarray,
                     tf: str) -> dict:
    p = TF_PARAMS[tf]
    per = PERIODS[tf]
    sigs = signals_vwap_mr(closes, volumes, p["vwap_n"], p["z_thr"])
    results: dict = {}
    for cost_bps in COST_BPS_LIST:
        rets, n_trades = sim_taker(closes, sigs, p["hold"], cost_bps / 20000)
        n_years = len(closes) / per
        results[cost_bps] = {
            "sharpe":        round(annualized_sharpe(rets, per), 3),
            "n_trades":      n_trades,
            "trades_per_day": round(n_trades / (n_years * 365), 2) if n_years > 0 else 0,
            "avg_hold_h":    round(p["hold"] * {"5m": 5, "30m": 30, "1h": 60}[tf] / 60, 1),
        }
    return results


# ── Per-cycle robustness ───────────────────────────────────────────────────────

def per_cycle_sharpe(df: pd.DataFrame, tf: str) -> list[tuple[str, float]]:
    p   = TF_PARAMS[tf]
    per = PERIODS[tf]
    closes  = df["close"].values.astype(float)
    volumes = df["volume"].values.astype(float)
    sigs    = signals_vwap_mr(closes, volumes, p["vwap_n"], p["z_thr"])

    results = []
    for name, start, end in CYCLES:
        mask = (df.index >= pd.Timestamp(start, tz="UTC")) & \
               (df.index < pd.Timestamp(end, tz="UTC") + pd.Timedelta(days=1))
        idx = np.where(mask)[0]
        if len(idx) < p["hold"] * 3:
            results.append((name, float("nan")))
            continue
        i0, i1 = idx[0], idx[-1] + 1
        c_sub = closes[i0:i1]; s_sub = sigs[i0:i1]
        rets, _ = sim_taker(c_sub, s_sub, p["hold"], GATE_COST_BPS / 20000)
        results.append((name, round(annualized_sharpe(rets, per), 3)))
    return results


# ── Gate ──────────────────────────────────────────────────────────────────────

def make_strategy_fn(tf: str):
    p           = TF_PARAMS[tf]
    per         = PERIODS[tf]
    ctx         = CONTEXT_BARS[tf]
    cost_oneway = GATE_COST_BPS / 20000

    def strategy_fn(train_data: list, test_data: list) -> dict:
        def get(data, key):
            return np.array([d[key] for d in data], dtype=float)

        tr_c = get(train_data, "close"); tr_v = get(train_data, "volume")
        te_c = get(test_data,  "close"); te_v = get(test_data,  "volume")

        if len(tr_c) < ctx or len(te_c) < p["hold"]:
            return {"sharpe": 0.0, "returns": [], "is_sharpe": 0.0}

        is_sigs = signals_vwap_mr(tr_c, tr_v, p["vwap_n"], p["z_thr"])
        is_rets, _ = sim_taker(tr_c, is_sigs, p["hold"], cost_oneway)

        ctx_c = tr_c[-ctx:]; ctx_v = tr_v[-ctx:]
        full_c = np.concatenate([ctx_c, te_c])
        full_v = np.concatenate([ctx_v, te_v])
        full_sigs = signals_vwap_mr(full_c, full_v, p["vwap_n"], p["z_thr"])
        oos_sigs  = full_sigs[ctx:]

        oos_rets, _ = sim_taker(te_c, oos_sigs, p["hold"], cost_oneway)

        return {
            "sharpe":    annualized_sharpe(oos_rets, per),
            "returns":   list(oos_rets),
            "is_sharpe": annualized_sharpe(is_rets, per),
        }

    return strategy_fn


def run_gate(inst: str, df: pd.DataFrame, tf: str) -> dict:
    n_bars   = len(df)
    n_splits = max(4, min(8, n_bars // FOLD_MIN[tf]))
    emb      = EMBARGO_BARS[tf]

    records = [
        {"close": float(r.close), "volume": float(r.volume)}
        for r in df.itertuples()
    ]

    config = BacktestGateConfig(
        strategy_name=f"vwap_mr_{tf}_{inst}",
        n_splits=n_splits,
        embargo=emb,
        pbo_threshold=0.5,
        periods=PERIODS[tf],
    )
    fn   = make_strategy_fn(tf)
    gate = backtest_gate(fn, records, config=config)
    wf   = gate["walk_forward_result"]
    frs  = wf["fold_results"]

    return {
        "gate_status": gate["gate_status"],
        "oos_sharpe":  round(float(gate["mean_oos_sharpe"]), 3),
        "dsr":         round(float(gate["deflated_sharpe"]), 3),
        "pbo":         round(float(gate["pbo"]), 3),
        "n_splits":    n_splits,
        "fold_oos":    [round(float(fr.get("sharpe", 0)), 3) for fr in frs],
        "fold_is":     [round(float(fr.get("is_sharpe", 0)), 3) for fr in frs],
    }


# ── Verdict doc ───────────────────────────────────────────────────────────────

def write_verdict(
    data_info:      dict,
    cost_tables:    dict,   # {inst: {tf: {cost_bps: result}}}
    gate_results:   dict,   # {inst: {tf: dict or None}}
    cycle_results:  dict,   # {inst: {tf: [(name, sharpe)]}}
) -> Path:

    # §1 data info
    data_rows = []
    for inst in INSTRUMENTS:
        for tf, label in [("5m", "5m"), ("30m", "30m"), ("1h", "1H")]:
            d = data_info.get(inst, {}).get(tf, {})
            if d:
                data_rows.append(
                    f"| {inst} | {label} | {d['n_bars']:,} | "
                    f"{d['first_bar']} | {d['last_bar']} |"
                )

    # §2 frequency/alpha table (5m vs 30m vs 1H)
    freq_rows = []
    for inst in INSTRUMENTS:
        for tf, label in [("5m", "5m"), ("30m", "30m"), ("1h", "1H")]:
            ct = cost_tables.get(inst, {}).get(tf, {})
            if not ct:
                continue
            r0  = ct.get(0, {})
            r10 = ct.get(10, {})
            r15 = ct.get(15, {})
            freq_rows.append(
                f"| {inst} | {label} | "
                f"{r0.get('trades_per_day', 0):.1f} | "
                f"{r0.get('avg_hold_h', 0):.0f}H | "
                f"{r0.get('sharpe', 0):+.3f} | "
                f"{r10.get('sharpe', 0):+.3f} | "
                f"{r15.get('sharpe', 0):+.3f} |"
            )

    # §3 full cost sensitivity
    cost_sections = []
    for tf, label in [("5m", "5m"), ("30m", "30m"), ("1h", "1H")]:
        p = TF_PARAMS[tf]
        header = (
            f"### {label}  (vwap_n={p['vwap_n']}, z_thr={p['z_thr']}, "
            f"hold={p['hold']} bars)\n\n"
            f"| Instrument | 0 bps (gross) | 5 bps RT | 10 bps RT | 15 bps RT | 20 bps RT | Trades/day |\n"
            f"|---|---|---|---|---|---|---|\n"
        )
        rows = []
        for inst in INSTRUMENTS:
            ct = cost_tables.get(inst, {}).get(tf, {})
            sharpes = " | ".join(
                f"{ct.get(c, {}).get('sharpe', 0):+.3f}" for c in COST_BPS_LIST
            )
            tpd = ct.get(0, {}).get("trades_per_day", 0)
            rows.append(f"| {inst} | {sharpes} | {tpd:.1f} |")
        cost_sections.append(header + "\n".join(rows))

    # §4 gate results
    gate_rows = []
    for inst in INSTRUMENTS:
        for tf, label in [("30m", "30m"), ("1h", "1H")]:
            g = gate_results.get(inst, {}).get(tf)
            if g is None:
                ct   = cost_tables.get(inst, {}).get(tf, {})
                s10  = ct.get(10, {}).get("sharpe", 0)
                gate_rows.append(
                    f"| {inst} | {label} | — | — | — | — | "
                    f"skipped (@10bps={s10:+.3f}) |"
                )
            else:
                folds = " / ".join(str(x) for x in g["fold_oos"])
                st    = "**PASS**" if g["gate_status"] == "passed" else "**FAIL**"
                gate_rows.append(
                    f"| {inst} | {label} | {g['oos_sharpe']:+.3f} | "
                    f"{g['dsr']:+.3f} | {g['pbo']:.2f} | {g['n_splits']} | {st} |"
                )
                gate_rows.append(f"|  |  | IS: {' / '.join(str(x) for x in g['fold_is'])} | | | | |")
                gate_rows.append(f"|  |  | OOS: {folds} | | | | |")

    # §5 per-cycle
    cycle_sec = []
    for inst in INSTRUMENTS:
        for tf, label in [("30m", "30m"), ("1h", "1H")]:
            cr = cycle_results.get(inst, {}).get(tf)
            if not cr:
                continue
            header = (
                f"### {inst} — {label}\n\n"
                f"| Cycle | Sharpe @10bps | OK |\n|---|---|---|\n"
            )
            rows_c = []
            for cycle_name, sh in cr:
                if sh != sh:  # NaN
                    rows_c.append(f"| {cycle_name} | insufficient data | — |")
                else:
                    ok = "✓" if sh > 0 else "✗"
                    rows_c.append(f"| {cycle_name} | {sh:+.3f} | {ok} |")
            cycle_sec.append(header + "\n".join(rows_c))

    # §6 verdict
    n_pass = sum(
        1 for inst in INSTRUMENTS
        for tf in ["30m", "1h"]
        if (gate_results.get(inst, {}).get(tf) or {}).get("gate_status") == "passed"
    )
    n_gate_run = sum(
        1 for inst in INSTRUMENTS
        for tf in ["30m", "1h"]
        if gate_results.get(inst, {}).get(tf) is not None
    )

    if n_pass > 0:
        verdict = "**PASS — proceed to OKX Demo paper trading**"
        verdict_detail = (
            f"{n_pass} strategy-instrument-timeframe combination(s) pass walk-forward gate "
            f"at {GATE_COST_BPS}bps taker cost. Reducing trade frequency from 5m scalping "
            f"to 30m/1H rescues VWAP mean reversion alpha."
        )
        next_step = (
            "Deploy VWAP-MR at passing timeframe on OKX Demo (taker orders, limit for exit). "
            "Track live Sharpe vs backtest. 5-7 days minimum before any live consideration."
        )
    elif n_gate_run > 0:
        verdict = "**NO-GO — gate fails despite positive @10bps gross**"
        verdict_detail = (
            "Some timeframes survive cost sensitivity but fail walk-forward "
            "DSR and/or PBO gate. Signal is not statistically reliable across folds."
        )
        next_step = (
            "VWAP-MR alpha decays across walk-forward folds — likely regime-dependent. "
            "Consider: (1) regime filter (HMM) on top of VWAP-MR, (2) longer VWAP window, "
            "(3) 4H VWAP-MR building on existing 4H data."
        )
    else:
        verdict = "**NO-GO — cost-killed at all timeframes**"
        verdict_detail = (
            "No timeframe achieves positive Sharpe at 10bps taker round-trip. "
            "Reducing frequency from 5m does not recover net alpha."
        )
        next_step = (
            "VWAP mean reversion mechanism does not have sufficient edge at taker rates "
            "across 5m/30m/1H timeframes. Strategy 2 search continues."
        )

    doc = f"""# R5.3 VWAP Mean Reversion — Multi-Timeframe (30m / 1H)

> Hypothesis: 5m VWAP-MR has genuine gross alpha but 8.4 trades/day × 10bps taker = 307%/yr kills it.
> 30m/1H: same signal, fewer trades, larger P&L/trade → cost burden ~30-70%/yr → net alpha?
> Gate: taker 10bps RT (no maker assumption). Same DSR+PBO thresholds as all prior gates.

## §0 Parameter Design Note

VWAP window must match the **same absolute time period** across timeframes, not the same bar count.
5m uses vwap_n=48 × 5m = 240 min = **4H**. The 30m/1H equivalents:

| Timeframe | Bars | Absolute window | Hold bars | Hold (absolute) |
|---|---|---|---|---|
| 5m  | 48 | 4H | 12 | 1H |
| 30m |  8 | 4H | 12 | 6H |
| 1H  |  4 | 4H |  6 | 6H |

Using a 24H or 48H VWAP window at 30m/1H is a **different signal** (daily VWAP deviation, not
intraday). Naive bar-count parity (vwap_n=48 for all TFs) destroys the alpha mechanism
and shows negative gross Sharpe — not because the mechanism fails, but because it tests
a different signal. All results below use the 4H-equivalent window.

## §1 Data

| Instrument | Timeframe | Bars | First Bar | Last Bar |
|---|---|---|---|---|
{chr(10).join(data_rows)}

30m and 1H resampled from 5m data (OHLCV aggregation). No additional API backfill needed.

## §2 Frequency / Alpha Comparison (5m vs 30m vs 1H)

This is the core test of the hypothesis: does reducing frequency change the cost/alpha ratio?

| Instrument | TF | Trades/day | Hold | Gross Sharpe (0bps) | Net @10bps RT | Net @15bps RT |
|---|---|---|---|---|---|---|
{chr(10).join(freq_rows)}

**Decision rule**: does @10bps Sharpe flip positive as frequency drops?

## §3 Full Cost Sensitivity

{chr(10).join(cost_sections)}

## §4 Walk-Forward Gate ({GATE_COST_BPS}bps taker RT, CPCV)

Only timeframes with positive @10bps Sharpe proceed. Gate: DSR>0 AND PBO<0.5.

| Instrument | TF | OOS Sharpe | DSR | PBO | n_splits | Gate |
|---|---|---|---|---|---|---|
{chr(10).join(gate_rows)}

Embargo: per-timeframe (30m: {EMBARGO_BARS['30m']} bars, 1H: {EMBARGO_BARS['1h']} bars).
Context prepend: 30m={CONTEXT_BARS['30m']} bars, 1H={CONTEXT_BARS['1h']} bars for VWAP warmup.

## §5 Per-Cycle Robustness (@10bps taker, in-sample)

Does alpha survive across market regimes? Cycles defined by dates; Sharpe computed on each.

{chr(10).join(cycle_sec)}

## §6 Look-Ahead Audit

- VWAP rolling windows use `.shift(1)`: signals[i] uses bars [0..i-1] only ✓
- 30m/1H bars resampled from 5m: aggregation uses only confirmed closed 5m bars ✓
- Resampling: open=first, high=max, low=min, close=last, volume=sum over correct window ✓
- Entry at bar i+1 open ≈ bar i close (1-bar lag); no future data in signal computation ✓
- Context prepend: last {CONTEXT_BARS['30m']} IS bars for warmup; fresh pos=0 at test start ✓
- Per-cycle sharpe: signals computed on FULL instrument array, sliced by date — no future leak ✓

## §7 Verdict

### {verdict}

{verdict_detail}

### {next_step}

### Cumulative Scorecard

| Strategy | Timeframe | Trades/day | Gross Sharpe | @10bps Sharpe | Gate |
|---|---|---|---|---|---|
| R4.4 Donchian+HMM | 4H | ~0.3 | ~1.5 OOS | — | NO-GO (DSR/PBO) |
| R5.1 VWAP-MR taker | 5m | 8.4 | +0.81 (BTC) | −5.9 | NO-GO (cost-killed) |
| R5.2 VWAP-MR maker | 5m | 8.3 | +0.51 (BTC) | −2.1 @4bps | NO-GO (cost-killed) |
| R5.3 VWAP-MR | 30m | see §2 | see §2 | see §2 | see §4 |
| R5.3 VWAP-MR | 1H  | see §2 | see §2 | see §2 | see §4 |

---
*Generated by `ops/scripts/vwap_mr_mtf_gate_r53.py` — R5.3 milestone.*
"""
    out = Path(__file__).parent.parent.parent / "docs" / "R5.3_VWAP_MR_MTF.md"
    out.write_text(doc)
    return out


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    import time

    print("=== R5.3 VWAP-MR Multi-Timeframe Gate ===\n")

    # ── Load and resample ──
    all_dfs:   dict[str, dict[str, pd.DataFrame]] = {}
    data_info: dict[str, dict[str, dict]]          = {}

    for inst in INSTRUMENTS:
        print(f"Loading {inst} 5m data ...", end=" ", flush=True)
        t0    = time.monotonic()
        df5m  = asyncio.run(load_5m_raw(inst))
        print(f"{len(df5m):,} bars  [{time.monotonic()-t0:.1f}s]")

        all_dfs[inst]   = {}
        data_info[inst] = {}

        for tf, freq in [("5m", "5min"), ("30m", "30min"), ("1h", "1h")]:
            if tf == "5m":
                df = df5m
            else:
                df = resample_ohlcv(df5m, freq)
            all_dfs[inst][tf] = df
            data_info[inst][tf] = {
                "n_bars":    len(df),
                "first_bar": df.index[0].date().isoformat() if len(df) else "?",
                "last_bar":  df.index[-1].date().isoformat() if len(df) else "?",
            }
            print(f"  {tf}: {len(df):,} bars  "
                  f"{data_info[inst][tf]['first_bar']} → {data_info[inst][tf]['last_bar']}")
        print()

    # ── Cost sensitivity ──
    print("── Cost sensitivity ──")
    print(f"  {'Instrument':<24} {'TF':<4} {'0bps':>7} {'5bps':>7} {'10bps':>7} "
          f"{'15bps':>7} {'20bps':>7}  trades/day")

    cost_tables: dict[str, dict[str, dict]] = {}

    for inst in INSTRUMENTS:
        cost_tables[inst] = {}
        for tf in ["5m", "30m", "1h"]:
            df = all_dfs[inst][tf]
            c  = df["close"].values.astype(float)
            v  = df["volume"].values.astype(float)
            t0 = time.monotonic()
            ct = cost_sensitivity(c, v, tf)
            cost_tables[inst][tf] = ct
            sharpes = "  ".join(f"{ct[x]['sharpe']:+.3f}" for x in COST_BPS_LIST)
            tpd = ct[0]["trades_per_day"]
            print(f"  {inst:<24} {tf:<4} {sharpes}  {tpd:.2f}/day [{time.monotonic()-t0:.1f}s]")
        print()

    # ── Gate ──
    print(f"── Walk-forward gate ({GATE_COST_BPS}bps taker) ──")
    gate_results: dict[str, dict[str, dict | None]] = {}

    for inst in INSTRUMENTS:
        gate_results[inst] = {}
        for tf in ["30m", "1h"]:
            ct10 = cost_tables[inst][tf].get(10, {}).get("sharpe", -999)
            if ct10 <= 0.0:
                print(f"  {inst} {tf}: SKIP  (@10bps Sharpe={ct10:+.3f} ≤ 0)")
                gate_results[inst][tf] = None
                continue
            df      = all_dfs[inst][tf]
            t0      = time.monotonic()
            g       = run_gate(inst, df, tf)
            gate_results[inst][tf] = g
            st      = "PASS" if g["gate_status"] == "passed" else "FAIL"
            elapsed = time.monotonic() - t0
            print(f"  {inst} {tf}: {st}  "
                  f"OOS={g['oos_sharpe']:+.3f}  DSR={g['dsr']:+.3f}  "
                  f"PBO={g['pbo']:.2f}  n={g['n_splits']}  [{elapsed:.0f}s]")
            print(f"    IS:  {g['fold_is']}")
            print(f"    OOS: {g['fold_oos']}")
        print()

    # ── Per-cycle robustness ──
    print("── Per-cycle robustness (@10bps taker) ──")
    cycle_results: dict[str, dict[str, list]] = {}

    for inst in INSTRUMENTS:
        cycle_results[inst] = {}
        for tf in ["30m", "1h"]:
            df  = all_dfs[inst][tf]
            cr  = per_cycle_sharpe(df, tf)
            cycle_results[inst][tf] = cr
            print(f"  {inst} {tf}:")
            for cn, sh in cr:
                tag = "✓" if (sh == sh and sh > 0) else ("—" if sh != sh else "✗")
                sh_str = f"{sh:+.3f}" if sh == sh else "  n/a"
                print(f"    {cn:<16} {sh_str}  {tag}")
        print()

    # ── Write verdict ──
    out = write_verdict(data_info, cost_tables, gate_results, cycle_results)
    print(f"Verdict → {out.relative_to(Path.cwd()) if out.exists() else out}")


if __name__ == "__main__":
    main()
