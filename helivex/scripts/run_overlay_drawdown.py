"""§7.3 headline gate (REAL DATA) — overlay drawdown reduction vs naked book.

The overlay's success metric is NOT DSR (it's a beta book) — it is whether the
regime overlay REDUCES max drawdown vs the SAME vol-target base book WITHOUT the
overlay. This driver runs both through the real v5-1 simulator on funded parquets
(written by run_features.py) and reports the drawdown pair + UNSTABLE exposure.

  overlaid = RiskOffOverlay(OverlayConfig.for_timeframe("1h"))      # full overlay
  naked    = RiskOffOverlay(OverlayConfig.naked_baseline("1h"))     # all regime terms OFF

Only difference between the two = the regime overlay itself (risk_multiplier
throttle + NEUTRAL damping + UNSTABLE kill-switch). Same base bias, same vol
targeting, same costs.

Usage:
    python -m helivex.scripts.run_overlay_drawdown data/smoke_ftx_funded_1h.parquet \
        data/smoke_covid_funded_1h.parquet data/smoke_calm2023_funded_1h.parquet
"""
from __future__ import annotations

import argparse
from pathlib import Path

from helios_backtest_v51 import CostModel, simulate

from helivex_overlay import OverlayConfig, RiskOffOverlay
from helivex.scripts.run_backtest_v51 import load_bars


def _max_drawdown(curve: list[float]) -> float:
    if not curve:
        return 0.0
    peak, mdd = curve[0], 0.0
    for v in curve:
        peak = max(peak, v)
        mdd = max(mdd, (peak - v) / peak if peak > 0 else 0.0)
    return mdd


def _run_one(path: Path, cost: CostModel) -> dict:
    bars = load_bars(path)
    n_unstable = sum(1 for b in bars if b.features.get("regime") == "unstable")

    overlaid = RiskOffOverlay(OverlayConfig.for_timeframe("1h"))
    naked = RiskOffOverlay(OverlayConfig.naked_baseline("1h"))
    overlaid.reset()
    naked.reset()

    res_o = simulate(overlaid, bars, cost_model=cost)
    res_n = simulate(naked, bars, cost_model=cost)
    rep = overlaid.report()

    return {
        "label": path.stem,
        "n_bars": len(bars),
        "n_unstable": n_unstable,
        "dd_overlaid": _max_drawdown(res_o.equity_curve),
        "dd_naked": _max_drawdown(res_n.equity_curve),
        "eq_overlaid": res_o.equity_curve[-1] if res_o.equity_curve else 1.0,
        "eq_naked": res_n.equity_curve[-1] if res_n.equity_curve else 1.0,
        "expo_unstable": rep.mean_exposure_unstable,
        "expo_stable": rep.mean_exposure_stable,
        "time_in_market": rep.time_in_market,
        "kill_frac": rep.kill_switch_fraction,
        "disclaimer": rep.beta_disclaimer,
    }


def main() -> None:
    p = argparse.ArgumentParser(description="§7.3 overlay drawdown gate (real data)")
    p.add_argument("data", nargs="+", help="funded parquet(s) from run_features")
    p.add_argument("--taker-bps", type=float, default=5.0)
    p.add_argument("--slip-bps", type=float, default=1.0)
    args = p.parse_args()

    cost = CostModel(taker_fee_bps=args.taker_bps, fixed_slippage_bps=args.slip_bps)
    results = [_run_one(Path(d).expanduser(), cost) for d in args.data]

    print(f"\nOverlay drawdown gate — cost: taker {args.taker_bps}bps + slip {args.slip_bps}bps")
    print(f"BETA DISCLAIMER: {results[0]['disclaimer']}")
    print("=" * 78)
    print(f"{'window':24s} {'bars':>5} {'UNST':>5} {'maxDD_ovl':>10} {'maxDD_nkd':>10} "
          f"{'DDcut%':>7} {'exp_UNST':>9} {'verdict':>9}")
    print("-" * 78)
    for r in results:
        cut = (1.0 - r["dd_overlaid"] / r["dd_naked"]) * 100 if r["dd_naked"] > 0 else 0.0
        # gate per window: overlay reduces DD AND flattens in UNSTABLE
        ok = (r["dd_overlaid"] < r["dd_naked"]) and (r["expo_unstable"] < 1e-9 or r["n_unstable"] == 0)
        print(f"{r['label']:24s} {r['n_bars']:>5} {r['n_unstable']:>5} "
              f"{r['dd_overlaid']:>10.4f} {r['dd_naked']:>10.4f} {cut:>6.1f}% "
              f"{r['expo_unstable']:>9.4f} {'PASS' if ok else 'FAIL':>9}")
    print("=" * 78)
    print("Read the maxDD pair (NOT a Sharpe): overlay PASSES when maxDD_ovl < maxDD_nkd")
    print("and exposure during UNSTABLE ≈ 0. Calm window should show small DDcut (overlay")
    print("only bites in tails). DSR REJECT under v5-1 is EXPECTED for a beta book.")


if __name__ == "__main__":
    main()
