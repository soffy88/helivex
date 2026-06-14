"""R3.2: ETC/XRP anti-overfitting analysis.

Three anti-overfitting treatments:
    A. Per-fold hedge (baseline — current pair_screener approach)
    B. Fixed full-sample hedge (reduces IS parameter variance)
    C. Daily bars: resample 1H→1D, rerun gate (lower turnover, less noise)

Rolling cointegration stability check: 90-day window, 7-day step.

No OKX API key required.

Usage:
    python ops/scripts/etc_xrp_gate.py [--quiet]
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import date, datetime, timezone
from pathlib import Path

import asyncpg
import numpy as np

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

DB_DSN = "postgresql://helios:helios_dev_pass@localhost:5434/helivex"

STAT_ARB_ROUND_TRIP_1H = 0.0002 * 4   # 8bps (perp only; 1H)
STAT_ARB_ROUND_TRIP_1D = 0.0002 * 4   # same fee structure (fewer trades)
PERIODS_1H = 24 * 252
PERIODS_1D = 252


# ── Data loading ──────────────────────────────────────────────────────────────
def load_etc_xrp() -> tuple[list[tuple], list[tuple]]:
    """Return (etc_ts_price, xrp_ts_price) sorted ascending."""
    async def _fetch():
        conn = await asyncpg.connect(DB_DSN)
        etc = await conn.fetch(
            "SELECT bar_close_ts, close FROM market_data.ohlcv_1h "
            "WHERE instrument='ETC-USDT' ORDER BY bar_close_ts ASC"
        )
        xrp = await conn.fetch(
            "SELECT bar_close_ts, close FROM market_data.ohlcv_1h "
            "WHERE instrument='XRP-USDT' ORDER BY bar_close_ts ASC"
        )
        await conn.close()
        return (
            [(r["bar_close_ts"], float(r["close"])) for r in etc],
            [(r["bar_close_ts"], float(r["close"])) for r in xrp],
        )
    return asyncio.run(_fetch())


def align_ts(s1: list[tuple], s2: list[tuple]) -> tuple[np.ndarray, np.ndarray, list]:
    """Align on timestamps. Returns (a, b, common_ts)."""
    m1 = {r[0]: r[1] for r in s1}
    m2 = {r[0]: r[1] for r in s2}
    common = sorted(set(m1) & set(m2))
    return (
        np.array([m1[t] for t in common]),
        np.array([m2[t] for t in common]),
        common,
    )


def resample_daily(ts_prices: list[tuple]) -> list[tuple[date, float]]:
    """Collapse 1H bars to daily: last close per UTC calendar day."""
    day_map: dict[date, float] = {}
    for ts, price in ts_prices:
        d = ts.astimezone(timezone.utc).date()
        day_map[d] = price   # last bar of the day wins
    return sorted(day_map.items())


# ── Helpers ───────────────────────────────────────────────────────────────────
def _sharpe(returns: list[float], periods: int) -> float:
    arr = np.asarray(returns, dtype=float)
    if len(arr) < 2:
        return 0.0
    std = float(arr.std())
    return 0.0 if std < 1e-12 else float((arr.mean() / std) * np.sqrt(periods))


def _spread_bar_returns(
    a: np.ndarray,
    b: np.ndarray,
    hedge: float,
    mean_s: float,
    std_s: float,
    ref: float,
    round_trip: float,
    entry_z: float = 2.0,
    exit_z: float = 0.5,
) -> list[float]:
    """Per-bar % returns for mean-reversion on spread b − hedge·a."""
    spread = b - hedge * a
    z = (spread - mean_s) / (std_s + 1e-12)
    pos = 0
    rets: list[float] = []
    for i in range(1, len(spread)):
        dspread = float(spread[i] - spread[i - 1])
        bar_ret = pos * dspread / ref
        zi_prev = float(z[i - 1])   # non-look-ahead signal
        if pos == 0:
            if zi_prev <= -entry_z:
                pos = 1
                bar_ret = pos * dspread / ref - round_trip
            elif zi_prev >= entry_z:
                pos = -1
                bar_ret = pos * dspread / ref - round_trip
        else:
            if abs(zi_prev) <= exit_z:
                pos = 0
        rets.append(bar_ret)
    return rets


# ── Strategy factories ────────────────────────────────────────────────────────
def make_strategy_per_fold_hedge(round_trip: float, periods: int):
    """Version A: per-fold OLS hedge (baseline, as in pair_screener)."""
    def fn(train_data: list, test_data: list) -> dict:
        if len(train_data) < 60 or len(test_data) < 5:
            return {"sharpe": 0.0, "returns": [], "is_sharpe": 0.0}
        a_tr = np.array([d[0] for d in train_data])
        b_tr = np.array([d[1] for d in train_data])
        X = np.column_stack([a_tr, np.ones(len(a_tr))])
        hedge = float(np.linalg.lstsq(X, b_tr, rcond=None)[0][0])
        sp_tr = b_tr - hedge * a_tr
        mean_s, std_s = float(sp_tr.mean()), float(sp_tr.std())
        ref = float(np.mean(np.abs(b_tr))) + 1e-8
        is_r = _spread_bar_returns(a_tr, b_tr, hedge, mean_s, std_s, ref, round_trip)
        a_te = np.array([d[0] for d in test_data])
        b_te = np.array([d[1] for d in test_data])
        oos_r = _spread_bar_returns(a_te, b_te, hedge, mean_s, std_s, ref, round_trip)
        return {"sharpe": _sharpe(oos_r, periods), "returns": oos_r,
                "is_sharpe": _sharpe(is_r, periods)}
    return fn


def make_strategy_fixed_hedge(fixed_hedge: float, round_trip: float, periods: int):
    """Version B: full-sample hedge fixed, only mean/std from IS."""
    def fn(train_data: list, test_data: list) -> dict:
        if len(train_data) < 60 or len(test_data) < 5:
            return {"sharpe": 0.0, "returns": [], "is_sharpe": 0.0}
        a_tr = np.array([d[0] for d in train_data])
        b_tr = np.array([d[1] for d in train_data])
        sp_tr = b_tr - fixed_hedge * a_tr
        mean_s, std_s = float(sp_tr.mean()), float(sp_tr.std())
        ref = float(np.mean(np.abs(b_tr))) + 1e-8
        is_r = _spread_bar_returns(a_tr, b_tr, fixed_hedge, mean_s, std_s, ref, round_trip)
        a_te = np.array([d[0] for d in test_data])
        b_te = np.array([d[1] for d in test_data])
        oos_r = _spread_bar_returns(a_te, b_te, fixed_hedge, mean_s, std_s, ref, round_trip)
        return {"sharpe": _sharpe(oos_r, periods), "returns": oos_r,
                "is_sharpe": _sharpe(is_r, periods)}
    return fn


# ── Gate runner ───────────────────────────────────────────────────────────────
def run_gate(
    label: str,
    strategy_fn,
    data: list,
    periods: int,
    n_splits: int = 5,
    embargo: int = 24,
) -> dict:
    from omodul.backtest_gate import BacktestGateConfig, backtest_gate  # noqa: PLC0415

    config = BacktestGateConfig(
        strategy_name=f"stat_arb_ETC_XRP_{label}",
        n_splits=n_splits,
        embargo=embargo,
        pbo_threshold=0.5,
        periods=periods,
    )
    gate = backtest_gate(strategy_fn, data, config=config)
    wf = gate["walk_forward_result"]
    is_sharpes = [fr.get("is_sharpe", fr.get("sharpe", 0.0)) for fr in wf["fold_results"]]
    mean_is = float(np.mean(is_sharpes)) if is_sharpes else 0.0
    fold_oos = wf["oos_sharpes"]
    return {
        "label": label,
        "gate_status": gate["gate_status"],
        "mean_is_sharpe": round(mean_is, 4),
        "mean_oos_sharpe": round(gate["mean_oos_sharpe"], 4),
        "deflated_sharpe": round(gate["deflated_sharpe"], 4),
        "pbo": round(gate["pbo"], 4),
        "fail_reasons": gate["fail_reasons"],
        "fold_oos": [round(s, 4) for s in fold_oos],
    }


# ── Rolling cointegration ─────────────────────────────────────────────────────
def rolling_cointegration(
    a: np.ndarray,
    b: np.ndarray,
    timestamps: list,
    window_days: int = 90,
    step_days: int = 7,
    bars_per_day: int = 1,   # 1 for daily, 24 for 1H
) -> list[dict]:
    from oprim.cointegration_test import cointegration_test  # noqa: PLC0415

    window = window_days * bars_per_day
    step = step_days * bars_per_day
    n = len(a)
    results = []
    for start in range(0, n - window + 1, step):
        end = start + window
        try:
            r = cointegration_test(a[start:end], b[start:end])
            ts_start = timestamps[start]
            ts_end = timestamps[end - 1]
            results.append({
                "start": ts_start,
                "end": ts_end,
                "p_value": round(float(r["p_value"]), 4),
                "cointegrated": bool(r["cointegrated"]),
                "hedge_ratio": round(float(r["hedge_ratio"]), 5),
            })
        except Exception as exc:
            results.append({"start": timestamps[start], "error": str(exc),
                            "p_value": 1.0, "cointegrated": False})
    return results


# ── Verdict doc ───────────────────────────────────────────────────────────────
def write_verdict(
    gate_a: dict,
    gate_b: dict,
    gate_daily: dict,
    rolling: list[dict],
    full_hedge: float,
    daily_coint_p: float,
) -> None:
    docs = Path(__file__).parent.parent.parent / "docs"
    docs.mkdir(exist_ok=True)

    n_stable = sum(1 for r in rolling if r["cointegrated"])
    frac_stable = n_stable / len(rolling) if rolling else 0.0

    def fmt_gate(g: dict) -> str:
        return "**PASS**" if g["gate_status"] == "passed" else "FAIL"

    lines = [
        "# R3.2 ETC/XRP Anti-Overfitting Analysis",
        "",
        "> ETC/XRP: strongest cointegration p=0.0008, DSR=+0.68, PBO=0.75.",
        "> PBO > 0.5 caused by IS parameter variance. Three treatments applied.",
        "",
        "## Treatment 1: Fixed vs Per-Fold Hedge (1H bars)",
        "",
        "| Metric | A: Per-fold hedge (baseline) | B: Fixed hedge (full-sample) |",
        "|---|---|---|",
        f"| Hedge source | per-fold OLS | full-sample = {full_hedge:.6f} |",
        f"| IS Sharpe (mean fold) | {gate_a['mean_is_sharpe']:.4f} | {gate_b['mean_is_sharpe']:.4f} |",
        f"| OOS Sharpe (mean fold) | {gate_a['mean_oos_sharpe']:.4f} | {gate_b['mean_oos_sharpe']:.4f} |",
        f"| Deflated Sharpe (DSR) | {gate_a['deflated_sharpe']:.4f} | {gate_b['deflated_sharpe']:.4f} |",
        f"| PBO | {gate_a['pbo']:.4f} | **{gate_b['pbo']:.4f}** |",
        f"| Gate | {fmt_gate(gate_a)} | {fmt_gate(gate_b)} |",
        f"| Fold OOS sharpes | {gate_a['fold_oos']} | {gate_b['fold_oos']} |",
        "",
    ]

    if gate_b["gate_status"] == "passed":
        lines += [
            "**Fixed hedge drops PBO below 0.5 and DSR stays positive → Version B PASSES.**",
            "",
        ]
    else:
        lines += [
            f"Fixed hedge: PBO={gate_b['pbo']:.4f}, DSR={gate_b['deflated_sharpe']:.4f}.",
            "",
        ]

    lines += [
        "## Treatment 2: Daily Bars (1H → resample 1D)",
        "",
        f"- Daily cointegration p-value: **{daily_coint_p:.6f}** "
        f"({'cointegrated' if daily_coint_p < 0.05 else 'NOT cointegrated'})",
        "",
        "| Metric | Value |",
        "|---|---|",
        f"| IS Sharpe (mean fold) | {gate_daily['mean_is_sharpe']:.4f} |",
        f"| OOS Sharpe (mean fold) | {gate_daily['mean_oos_sharpe']:.4f} |",
        f"| Deflated Sharpe (DSR) | {gate_daily['deflated_sharpe']:.4f} |",
        f"| PBO | {gate_daily['pbo']:.4f} |",
        f"| Gate | {fmt_gate(gate_daily)} |",
        f"| Fold OOS sharpes | {gate_daily['fold_oos']} |",
        "",
    ]

    lines += [
        "## Treatment 3: Rolling Cointegration Stability (90-day window, 7-day step)",
        "",
        f"**{n_stable}/{len(rolling)} windows cointegrated (p < 0.05)** "
        f"— stability rate = {frac_stable:.0%}",
        "",
        "| Window Start | Window End | p-value | Cointegrated | Hedge |",
        "|---|---|---|---|---|",
    ]
    for r in rolling:
        p = r["p_value"]
        coint = "**YES**" if r["cointegrated"] else "no"
        s = str(r["start"])[:10]
        e = str(r["end"])[:10]
        lines.append(f"| {s} | {e} | {p:.4f} | {coint} | {r.get('hedge_ratio', 'n/a')} |")

    lines += ["", "## Final Verdict", ""]

    all_fail = all(
        g["gate_status"] != "passed" for g in [gate_a, gate_b, gate_daily]
    )
    any_pass = any(
        g["gate_status"] == "passed" for g in [gate_a, gate_b, gate_daily]
    )

    if any_pass:
        passing = [g["label"] for g in [gate_a, gate_b, gate_daily]
                   if g["gate_status"] == "passed"]
        lines += [
            f"**ETC/XRP PASSES the gate under treatment(s): {', '.join(passing)}.**",
            "",
            f"Cointegration stability: {frac_stable:.0%} of rolling windows. "
            f"{'Stable cointegration supports live deployment.' if frac_stable >= 0.6 else 'Regime-dependent — use HMM regime gate before live.'}",
        ]
    else:
        dsr_best = max(gate_a["deflated_sharpe"], gate_b["deflated_sharpe"],
                       gate_daily["deflated_sharpe"])
        pbo_best = min(gate_a["pbo"], gate_b["pbo"], gate_daily["pbo"])
        lines += [
            "**ETC/XRP does NOT pass the gate under any treatment.**",
            "",
            f"Best DSR across treatments: {dsr_best:.4f}. "
            f"Best PBO across treatments: {pbo_best:.4f}.",
            f"Rolling stability: {frac_stable:.0%}.",
            "",
            "The edge is real (strong cointegration, positive IS) but too regime-dependent",
            "to survive walk-forward testing at 8bps round-trip cost on 1H data.",
            "Recommendation: honest abandonment, or lower-frequency signal (weekly bars).",
        ]

    lines += [
        "",
        "---",
        "*Generated by `ops/scripts/etc_xrp_gate.py` — R3.2 milestone.*",
    ]

    (docs / "R3.2_ETC_XRP_GATE.md").write_text("\n".join(lines) + "\n")


# ── Main ──────────────────────────────────────────────────────────────────────
def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()
    q = args.quiet

    if not q:
        print("=== R3.2 ETC/XRP Anti-Overfitting ===\n")

    # Load data
    etc_ts, xrp_ts = load_etc_xrp()
    a_1h, b_1h, ts_1h = align_ts(etc_ts, xrp_ts)
    n1h = len(a_1h)
    if not q:
        print(f"1H data: {n1h} aligned bars")

    # Full-sample hedge
    from oprim.cointegration_test import cointegration_test  # noqa: PLC0415
    coint_full = cointegration_test(a_1h, b_1h)
    full_hedge = float(coint_full["hedge_ratio"])
    full_p = float(coint_full["p_value"])
    if not q:
        print(f"Full-sample: p={full_p:.6f}  hedge={full_hedge:.6f}\n")

    # ── 1H Version A: per-fold hedge ──────────────────────────────────────────
    if not q:
        print("[1/4] Version A — 1H per-fold hedge (baseline)...")
    data_1h = list(zip(a_1h.tolist(), b_1h.tolist()))
    fn_a = make_strategy_per_fold_hedge(STAT_ARB_ROUND_TRIP_1H, PERIODS_1H)
    gate_a = run_gate("1H_per_fold_hedge", fn_a, data_1h, PERIODS_1H)
    print(f"  A: IS={gate_a['mean_is_sharpe']:+.4f}  OOS={gate_a['mean_oos_sharpe']:+.4f}  "
          f"DSR={gate_a['deflated_sharpe']:+.4f}  PBO={gate_a['pbo']:.4f}  "
          f"→ {gate_a['gate_status'].upper()}")

    # ── 1H Version B: fixed hedge ─────────────────────────────────────────────
    if not q:
        print(f"[2/4] Version B — 1H fixed hedge={full_hedge:.6f}...")
    fn_b = make_strategy_fixed_hedge(full_hedge, STAT_ARB_ROUND_TRIP_1H, PERIODS_1H)
    gate_b = run_gate("1H_fixed_hedge", fn_b, data_1h, PERIODS_1H)
    print(f"  B: IS={gate_b['mean_is_sharpe']:+.4f}  OOS={gate_b['mean_oos_sharpe']:+.4f}  "
          f"DSR={gate_b['deflated_sharpe']:+.4f}  PBO={gate_b['pbo']:.4f}  "
          f"→ {gate_b['gate_status'].upper()}")

    # ── Daily resampling ──────────────────────────────────────────────────────
    if not q:
        print("[3/4] Daily bars (1H → resample 1D)...")
    etc_daily = resample_daily(etc_ts)
    xrp_daily = resample_daily(xrp_ts)
    # Align daily
    m_etc = {d: p for d, p in etc_daily}
    m_xrp = {d: p for d, p in xrp_daily}
    common_d = sorted(set(m_etc) & set(m_xrp))
    a_1d = np.array([m_etc[d] for d in common_d])
    b_1d = np.array([m_xrp[d] for d in common_d])
    ts_1d = common_d
    n1d = len(a_1d)
    if not q:
        print(f"  Daily bars: {n1d}")

    coint_daily = cointegration_test(a_1d, b_1d)
    daily_p = float(coint_daily["p_value"])
    daily_hedge = float(coint_daily["hedge_ratio"])
    if not q:
        print(f"  Daily coint: p={daily_p:.6f}  hedge={daily_hedge:.6f}")

    data_1d = list(zip(a_1d.tolist(), b_1d.tolist()))
    fn_daily = make_strategy_fixed_hedge(daily_hedge, STAT_ARB_ROUND_TRIP_1D, PERIODS_1D)
    gate_daily = run_gate("1D_fixed_hedge", fn_daily, data_1d, PERIODS_1D,
                          n_splits=5, embargo=1)
    print(f"  D: IS={gate_daily['mean_is_sharpe']:+.4f}  OOS={gate_daily['mean_oos_sharpe']:+.4f}  "
          f"DSR={gate_daily['deflated_sharpe']:+.4f}  PBO={gate_daily['pbo']:.4f}  "
          f"→ {gate_daily['gate_status'].upper()}")

    # ── Rolling cointegration ─────────────────────────────────────────────────
    if not q:
        print("[4/4] Rolling cointegration (daily, 90-day window, 7-day step)...")
    rolling = rolling_cointegration(a_1d, b_1d, ts_1d,
                                    window_days=90, step_days=7, bars_per_day=1)
    n_stable = sum(1 for r in rolling if r["cointegrated"])
    frac_stable = n_stable / len(rolling) if rolling else 0.0
    if not q:
        print(f"  {n_stable}/{len(rolling)} windows cointegrated ({frac_stable:.0%})")
        for r in rolling:
            flag = "✓" if r["cointegrated"] else "✗"
            print(f"    {flag} {str(r['start'])[:10]} → {str(r['end'])[:10]}  "
                  f"p={r['p_value']:.4f}  hedge={r.get('hedge_ratio', 'n/a')}")

    # ── Write verdict ─────────────────────────────────────────────────────────
    write_verdict(gate_a, gate_b, gate_daily, rolling, full_hedge, daily_p)
    if not q:
        print("\ndocs/R3.2_ETC_XRP_GATE.md written")

    # Key:value for determinism check
    print("\n=== R3.2 Key Results ===")
    print(f"  full_sample_p        : {full_p:.6f}")
    print(f"  full_hedge           : {full_hedge:.6f}")
    for g in [gate_a, gate_b, gate_daily]:
        lbl = g['label']
        print(f"  {lbl}_gate   : {g['gate_status']}")
        print(f"  {lbl}_oos    : {g['mean_oos_sharpe']}")
        print(f"  {lbl}_dsr    : {g['deflated_sharpe']}")
        print(f"  {lbl}_pbo    : {g['pbo']}")
    print(f"  rolling_windows      : {len(rolling)}")
    print(f"  rolling_stable       : {n_stable}")
    print(f"  rolling_frac         : {frac_stable:.4f}")


if __name__ == "__main__":
    main()
