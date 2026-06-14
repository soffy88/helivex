"""R3 alpha validation: funding_arb + stat_arb through backtest_gate.

Reads from market_data DB (no OKX API key required).
Both strategies are gated through omodul.backtest_gate (walk-forward + DSR + PBO).
Outputs key:value lines for determinism check and generates docs/R3_ALPHA_VERDICT.md.

Usage:
    python ops/scripts/alpha_gate.py [--quiet]
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import timezone
from pathlib import Path

import asyncpg
import numpy as np

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

DB_DSN = "postgresql://helios:helios_dev_pass@localhost:5434/helivex"

# ── Fee constants (OKX, as of 2026) ─────────────────────────────────────────
OKX_PERP_TAKER = 0.0002   # 0.02% taker per leg (perpetual swap)
OKX_SPOT_TAKER = 0.0010   # 0.10% taker per leg (spot)
# Funding arb: long spot + short perp — open + close each = 4 legs total
FUNDING_ARB_ROUND_TRIP = (OKX_SPOT_TAKER + OKX_PERP_TAKER) * 2   # 0.0024 = 24bps
# Stat arb: two perp legs, both sides opened and closed
STAT_ARB_ROUND_TRIP = OKX_PERP_TAKER * 4                          # 0.0008 = 8bps

# ── Annualisation factors ────────────────────────────────────────────────────
FUNDING_PERIODS = 3 * 365   # 3 settlements/day × 365 days
STAT_ARB_PERIODS = 24 * 252 # 1H bars, 24 bars/day, ~252 trading days/year


# ── Data loading ─────────────────────────────────────────────────────────────
def load_data_sync() -> tuple[list[dict], list[dict], list[dict]]:
    async def _fetch():
        conn = await asyncpg.connect(DB_DSN)
        funding = await conn.fetch(
            """SELECT ts, funding_rate, realized_rate
               FROM market_data.funding_rates
               WHERE instrument = 'BTC-USDT-SWAP'
               ORDER BY ts ASC"""
        )
        btc = await conn.fetch(
            """SELECT bar_close_ts, close FROM market_data.ohlcv_1h
               WHERE instrument = 'BTC-USDT' ORDER BY bar_close_ts ASC"""
        )
        eth = await conn.fetch(
            """SELECT bar_close_ts, close FROM market_data.ohlcv_1h
               WHERE instrument = 'ETH-USDT' ORDER BY bar_close_ts ASC"""
        )
        await conn.close()
        return [dict(r) for r in funding], [dict(r) for r in btc], [dict(r) for r in eth]

    return asyncio.run(_fetch())


# ── Helpers ───────────────────────────────────────────────────────────────────
def compute_sharpe(returns: list[float], periods: int) -> float:
    arr = np.asarray(returns, dtype=float)
    if len(arr) < 2:
        return 0.0
    std = float(arr.std())
    if std < 1e-12:
        return 0.0
    return float((arr.mean() / std) * np.sqrt(periods))


def _stat_arb_returns(
    zscores: np.ndarray,
    eth_prices: np.ndarray,
    entry_z: float = 2.0,
    exit_z: float = 0.5,
) -> list[float]:
    """Compute per-bar pct returns for a mean-reversion z-score strategy."""
    n = len(zscores)
    position = 0  # +1 = long spread, -1 = short spread
    returns: list[float] = []

    for i in range(1, n):
        z = float(zscores[i])
        # Spread PnL normalised to ETH price (reference for position sizing)
        ref = float(eth_prices[i - 1]) if eth_prices[i - 1] > 0 else 1.0
        dz = float(zscores[i] - zscores[i - 1])
        spread_pct = dz / (abs(float(zscores[i - 1])) + 1.0) * 0.01  # ~1% of z-unit

        if position == 0:
            if z <= -entry_z:
                position = 1
                returns.append(position * spread_pct - STAT_ARB_ROUND_TRIP)
            elif z >= entry_z:
                position = -1
                returns.append(position * spread_pct - STAT_ARB_ROUND_TRIP)
            else:
                returns.append(0.0)
        else:
            if abs(z) <= exit_z:
                returns.append(position * spread_pct - STAT_ARB_ROUND_TRIP)
                position = 0
            else:
                returns.append(position * spread_pct)

    return returns


# ── Strategy functions (strategy_fn protocol for backtest_gate) ───────────────
def funding_arb_fn(train_data: list, test_data: list) -> dict:
    """(train_data, test_data) → {"sharpe": oos_sharpe, "returns": [...], "is_sharpe": is_sharpe}"""
    if not train_data or not test_data:
        return {"sharpe": 0.0, "returns": [], "is_sharpe": 0.0}

    # IS: amortize round-trip cost over the training holding period
    n_tr = len(train_data)
    cost_per_bar_tr = FUNDING_ARB_ROUND_TRIP / n_tr
    is_returns = [float(r["realized_rate"]) - cost_per_bar_tr for r in train_data]
    is_sharpe = compute_sharpe(is_returns, FUNDING_PERIODS)

    # OOS: apply same cost structure
    n_te = len(test_data)
    cost_per_bar_te = FUNDING_ARB_ROUND_TRIP / n_te
    oos_returns = [float(r["realized_rate"]) - cost_per_bar_te for r in test_data]
    oos_sharpe = compute_sharpe(oos_returns, FUNDING_PERIODS)

    return {"sharpe": oos_sharpe, "returns": oos_returns, "is_sharpe": is_sharpe}


def make_stat_arb_fn(data_pairs: list[tuple[float, float]]):
    """Closure that captures pair data for index-based strategy_fn."""

    def stat_arb_strategy_fn(train_data: list, test_data: list) -> dict:
        if len(train_data) < 10 or len(test_data) < 2:
            return {"sharpe": 0.0, "returns": [], "is_sharpe": 0.0}

        btc_tr = np.array([d[0] for d in train_data])
        eth_tr = np.array([d[1] for d in train_data])

        # Estimate hedge ratio via OLS on training data
        X = np.column_stack([btc_tr, np.ones(len(btc_tr))])
        hedge_ratio = float(np.linalg.lstsq(X, eth_tr, rcond=None)[0][0])

        spread_tr = eth_tr - hedge_ratio * btc_tr
        mean_s = float(spread_tr.mean())
        std_s = float(spread_tr.std()) + 1e-10

        # IS performance
        z_tr = (spread_tr - mean_s) / std_s
        is_rets = _stat_arb_returns(z_tr, eth_tr)
        is_sharpe = compute_sharpe(is_rets, STAT_ARB_PERIODS)

        # OOS performance (apply training hedge_ratio/params to test)
        btc_te = np.array([d[0] for d in test_data])
        eth_te = np.array([d[1] for d in test_data])
        spread_te = eth_te - hedge_ratio * btc_te
        z_te = (spread_te - mean_s) / std_s
        oos_rets = _stat_arb_returns(z_te, eth_te)
        oos_sharpe = compute_sharpe(oos_rets, STAT_ARB_PERIODS)

        return {"sharpe": oos_sharpe, "returns": oos_rets, "is_sharpe": is_sharpe}

    return stat_arb_strategy_fn


# ── Gate runners ──────────────────────────────────────────────────────────────
def run_funding_gate(funding_data: list[dict]) -> dict:
    from omodul.backtest_gate import BacktestGateConfig, backtest_gate

    config = BacktestGateConfig(
        strategy_name="funding_arb",
        n_splits=5,
        embargo=2,
        pbo_threshold=0.5,
        periods=FUNDING_PERIODS,
    )
    gate = backtest_gate(funding_arb_fn, funding_data, config=config)

    # Pre-fee vs post-fee (full sample)
    all_rates = [float(r["realized_rate"]) for r in funding_data]
    raw_sr = compute_sharpe(all_rates, FUNDING_PERIODS)
    net_rates = [r - FUNDING_ARB_ROUND_TRIP / len(all_rates) for r in all_rates]
    net_sr = compute_sharpe(net_rates, FUNDING_PERIODS)

    wf = gate["walk_forward_result"]
    is_sharpes = [fr.get("is_sharpe", fr.get("sharpe", 0.0)) for fr in wf["fold_results"]]
    mean_is = float(np.mean(is_sharpes)) if is_sharpes else 0.0

    return {
        "n_rows": len(funding_data),
        "gate_status": gate["gate_status"],
        "mean_is_sharpe": round(mean_is, 4),
        "mean_oos_sharpe": round(gate["mean_oos_sharpe"], 4),
        "deflated_sharpe": round(gate["deflated_sharpe"], 4),
        "pbo": round(gate["pbo"], 4),
        "raw_full_sr": round(raw_sr, 4),
        "net_full_sr": round(net_sr, 4),
        "fail_reasons": gate["fail_reasons"],
    }


def run_stat_arb_gate(btc_rows: list[dict], eth_rows: list[dict]) -> dict:
    from oprim.cointegration_test import cointegration_test
    from omodul.backtest_gate import BacktestGateConfig, backtest_gate

    # Align timestamps
    btc_map = {r["bar_close_ts"]: float(r["close"]) for r in btc_rows}
    eth_map = {r["bar_close_ts"]: float(r["close"]) for r in eth_rows}
    common_ts = sorted(set(btc_map) & set(eth_map))
    data_pairs = [(btc_map[ts], eth_map[ts]) for ts in common_ts]

    # Full-sample cointegration test
    btc_all = [d[0] for d in data_pairs]
    eth_all = [d[1] for d in data_pairs]
    coint = cointegration_test(btc_all, eth_all)
    p_value = float(coint["p_value"])
    cointegrated = bool(coint["cointegrated"])
    hedge_ratio = float(coint["hedge_ratio"])

    config = BacktestGateConfig(
        strategy_name="stat_arb_btc_eth",
        n_splits=5,
        embargo=24,   # 24-bar embargo for 1H data (1 trading day)
        pbo_threshold=0.5,
        periods=STAT_ARB_PERIODS,
    )
    strategy_fn = make_stat_arb_fn(data_pairs)
    gate = backtest_gate(strategy_fn, data_pairs, config=config)

    wf = gate["walk_forward_result"]
    is_sharpes = [fr.get("is_sharpe", fr.get("sharpe", 0.0)) for fr in wf["fold_results"]]
    mean_is = float(np.mean(is_sharpes)) if is_sharpes else 0.0

    return {
        "n_pairs": len(data_pairs),
        "btc_eth_p_value": round(p_value, 6),
        "btc_eth_cointegrated": cointegrated,
        "hedge_ratio": round(hedge_ratio, 6),
        "gate_status": gate["gate_status"],
        "mean_is_sharpe": round(mean_is, 4),
        "mean_oos_sharpe": round(gate["mean_oos_sharpe"], 4),
        "deflated_sharpe": round(gate["deflated_sharpe"], 4),
        "pbo": round(gate["pbo"], 4),
        "fail_reasons": gate["fail_reasons"],
    }


# ── Verdict doc ───────────────────────────────────────────────────────────────
def write_verdict(fa: dict, sa: dict) -> None:
    docs = Path(__file__).parent.parent.parent / "docs"
    docs.mkdir(exist_ok=True)

    def gate_icon(status: str) -> str:
        return "PASS" if status == "passed" else "FAIL"

    lines = [
        "# R3 Alpha Verdict",
        "",
        "> **Go / No-Go decision** for funding_arb and stat_arb strategies.",
        "> All numbers are OOS (out-of-sample) unless noted. Fees are OKX 2026 taker rates.",
        "",
        "## Fee Structure",
        "",
        "| Strategy | Entry | Exit | Round Trip |",
        "|---|---|---|---|",
        f"| funding_arb | spot taker 10bps + perp taker 2bps | same | **24 bps** |",
        f"| stat_arb    | perp taker 2bps × 2 legs | same | **8 bps** |",
        "",
        "## Results",
        "",
        "| Metric | funding_arb | stat_arb (BTC-ETH) |",
        "|---|---|---|",
        f"| Data rows | {fa['n_rows']} funding records (~95d) | {sa['n_pairs']} 1H bars (~12mo) |",
        f"| IS Sharpe (mean fold) | {fa['mean_is_sharpe']:.4f} | {sa['mean_is_sharpe']:.4f} |",
        f"| OOS Sharpe (mean fold) | {fa['mean_oos_sharpe']:.4f} | {sa['mean_oos_sharpe']:.4f} |",
        f"| Deflated Sharpe (DSR) | {fa['deflated_sharpe']:.4f} | {sa['deflated_sharpe']:.4f} |",
        f"| PBO | {fa['pbo']:.4f} | {sa['pbo']:.4f} |",
        f"| Gate | **{gate_icon(fa['gate_status'])}** | **{gate_icon(sa['gate_status'])}** |",
        "",
        "## Pre-Fee vs Post-Fee (funding_arb, full sample)",
        "",
        "| | Sharpe |",
        "|---|---|",
        f"| Pre-fee (raw funding rate) | {fa['raw_full_sr']:.4f} |",
        f"| Post-fee (net of 24bps round trip) | {fa['net_full_sr']:.4f} |",
        "",
        "## Cointegration (stat_arb)",
        "",
        f"- BTC-ETH Engle-Granger p-value: **{sa['btc_eth_p_value']:.6f}**",
        f"- Cointegrated (p < 0.05): **{sa['btc_eth_cointegrated']}**",
        f"- Hedge ratio (ETH ≈ hedge_ratio × BTC): **{sa['hedge_ratio']:.6f}**",
        "",
        "## Fail Reasons",
        "",
        f"- **funding_arb**: {'; '.join(fa['fail_reasons']) if fa['fail_reasons'] else 'none'}",
        f"- **stat_arb**: {'; '.join(sa['fail_reasons']) if sa['fail_reasons'] else 'none'}",
        "",
        "## Honest Conclusion",
        "",
    ]

    fa_pass = fa["gate_status"] == "passed"
    sa_pass = sa["gate_status"] == "passed"

    if not fa_pass and not sa_pass:
        lines += [
            "**No strategy passes the gate. Go/No-Go: NO-GO.**",
            "",
            "Neither strategy survives the combined DSR + PBO filter.",
            "Possible causes: insufficient history (funding only 95 days), thin signal",
            "relative to fees, or genuine absence of exploitable alpha at this data scale.",
            "Recommendation: extend funding history (need 12+ months), re-run gate.",
        ]
    elif fa_pass and not sa_pass:
        lines += [
            "**funding_arb passes; stat_arb fails. Conditional GO for funding_arb only.**",
            "",
            "Funding arb shows positive OOS DSR, suggesting the edge is real net of fees.",
            "Stat arb does not clear the gate — either cointegration is weak or the signal",
            "erodes after transaction costs at this frequency.",
        ]
    elif not fa_pass and sa_pass:
        lines += [
            "**stat_arb passes; funding_arb fails. Conditional GO for stat_arb only.**",
            "",
            "Stat arb shows positive OOS DSR on the BTC-ETH pair.",
            "Funding arb does not clear the gate — funding rates may be too low vs. 24bps cost.",
        ]
    else:
        lines += [
            "**Both strategies pass the gate. GO.**",
            "",
            "Both funding_arb and stat_arb show positive OOS DSR and PBO < 0.5.",
        ]

    lines += [
        "",
        "---",
        "*Generated by `ops/scripts/alpha_gate.py` — R3 milestone.*",
        "*Data: market_data.ohlcv_1h + market_data.funding_rates (OKX public API).*",
    ]

    (docs / "R3_ALPHA_VERDICT.md").write_text("\n".join(lines) + "\n")


# ── Main ──────────────────────────────────────────────────────────────────────
def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()

    if not args.quiet:
        print("=== R3 Alpha Gate ===\n")

    # Load data
    funding_data, btc_rows, eth_rows = load_data_sync()
    if not args.quiet:
        print(f"Loaded: {len(funding_data)} funding records, {len(btc_rows)} BTC bars, {len(eth_rows)} ETH bars")

    # Run gates
    if not args.quiet:
        print("\n[1/2] Running funding_arb gate (walk-forward 5-fold)...")
    fa = run_funding_gate(funding_data)

    if not args.quiet:
        print(f"  IS Sharpe: {fa['mean_is_sharpe']}  OOS Sharpe: {fa['mean_oos_sharpe']}")
        print(f"  DSR: {fa['deflated_sharpe']}  PBO: {fa['pbo']}  → {fa['gate_status'].upper()}")

    if not args.quiet:
        print("\n[2/2] Running stat_arb (BTC-ETH) gate (walk-forward 5-fold)...")
    sa = run_stat_arb_gate(btc_rows, eth_rows)

    if not args.quiet:
        print(f"  BTC-ETH cointegration p={sa['btc_eth_p_value']:.6f} cointegrated={sa['btc_eth_cointegrated']}")
        print(f"  IS Sharpe: {sa['mean_is_sharpe']}  OOS Sharpe: {sa['mean_oos_sharpe']}")
        print(f"  DSR: {sa['deflated_sharpe']}  PBO: {sa['pbo']}  → {sa['gate_status'].upper()}")

    # Write verdict doc
    write_verdict(fa, sa)
    if not args.quiet:
        print("\ndocs/R3_ALPHA_VERDICT.md written")

    # Print key:value lines for determinism check
    print("\n=== R3 Alpha Gate Results ===")
    print(f"  fa_n_rows            : {fa['n_rows']}")
    print(f"  fa_gate_status       : {fa['gate_status']}")
    print(f"  fa_mean_is_sharpe    : {fa['mean_is_sharpe']}")
    print(f"  fa_mean_oos_sharpe   : {fa['mean_oos_sharpe']}")
    print(f"  fa_deflated_sharpe   : {fa['deflated_sharpe']}")
    print(f"  fa_pbo               : {fa['pbo']}")
    print(f"  fa_raw_full_sr       : {fa['raw_full_sr']}")
    print(f"  fa_net_full_sr       : {fa['net_full_sr']}")
    print(f"  sa_n_pairs           : {sa['n_pairs']}")
    print(f"  sa_p_value           : {sa['btc_eth_p_value']}")
    print(f"  sa_cointegrated      : {sa['btc_eth_cointegrated']}")
    print(f"  sa_gate_status       : {sa['gate_status']}")
    print(f"  sa_mean_is_sharpe    : {sa['mean_is_sharpe']}")
    print(f"  sa_mean_oos_sharpe   : {sa['mean_oos_sharpe']}")
    print(f"  sa_deflated_sharpe   : {sa['deflated_sharpe']}")
    print(f"  sa_pbo               : {sa['pbo']}")


if __name__ == "__main__":
    main()
