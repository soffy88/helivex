"""Step 3 + 4 — drive helivex_signal through the helios_backtest_v51 engine.

DirectionalStrategy now natively satisfies helios_backtest_v51.StrategyInterface
(fingerprint / reset / on_bar(BarRow) / config_space), so no adapter is needed.

BacktestRunner is a thin helivex-side façade aligned to the REAL engine API:
it takes a parquet PATH (written by run_features.py, funding-correct), loads it
to list[BarRow], and calls run_validation(strategy, bars, config=config). There
is NO FundingModel — funding rides on BarRow.funding_rate (stamped only at
settlement bars by run_features) and the simulator accrues it per bar. Costs are
the CostModel inside BacktestConfig; no separate cost argument.

Usage:
    python -m helivex.scripts.run_backtest_v51 --data data/smoke_ftx_funded_1h.parquet
"""
from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd

from helios_backtest_v51 import (
    BacktestConfig,
    BarRow,
    CostModel,
    ValidationReport,
    run_validation,
)
from helios_backtest_v51.cpcv import CPCVConfig
from helivex_signal.config import SignalConfig
from helivex_signal.strategy import DirectionalStrategy


def load_bars(parquet_path: Path) -> list[BarRow]:
    """Parquet (funding-correct, from run_features) → list[BarRow].

    funding_rate is read straight through: run_features already zeroed every
    non-settlement bar, so no funding logic belongs here.
    """
    df = pd.read_parquet(parquet_path)
    bars: list[BarRow] = []
    for row in df.itertuples(index=False):
        bars.append(
            BarRow(
                bar_close_ts=pd.Timestamp(row.bar_close_ts).to_pydatetime(),
                open=float(row.open),
                high=float(row.high),
                low=float(row.low),
                close=float(row.close),
                close_return=float(row.log_return),
                funding_rate=float(row.funding_rate),
                features={
                    "realized_var": float(row.realized_var),
                    "tail_index": float(row.tail_index),
                    "regime": str(row.regime),
                    "risk_multiplier": float(row.risk_multiplier),
                },
            )
        )
    return bars


class BacktestRunner:
    """Façade: parquet path + strategy + config → ValidationReport."""

    def __init__(self, strategy, data: str | Path, config: BacktestConfig) -> None:
        self.strategy = strategy
        self.data = Path(data).expanduser()
        self.config = config

    def run(self) -> ValidationReport:
        bars = load_bars(self.data)
        self._n_bars = len(bars)
        return run_validation(self.strategy, bars, config=self.config)


def _grid_size(grid: dict) -> int:
    n = 1
    for vals in grid.values():
        n *= len(vals)
    return n


def main() -> None:
    p = argparse.ArgumentParser(description="Step 3/4: v5-1 validation of helivex_signal")
    p.add_argument("--data", required=True, help="joined parquet from run_features")
    p.add_argument("--taker-bps", type=float, default=5.0)
    p.add_argument("--slip-bps", type=float, default=1.0)
    args = p.parse_args()

    strategy = DirectionalStrategy(SignalConfig.for_timeframe("1h"))
    config = BacktestConfig(
        cpcv=CPCVConfig(n_groups=10, n_test_groups=2, embargo_bars=24),
        cost_model=CostModel(taker_fee_bps=args.taker_bps, fixed_slippage_bps=args.slip_bps),
    )
    runner = BacktestRunner(strategy, args.data, config)
    report = runner.run()

    print(f"Loaded {runner._n_bars} bars from {args.data}")
    print(f"CPCV: C({config.cpcv.n_groups},{config.cpcv.n_test_groups}) splits, "
          f"embargo={config.cpcv.embargo_bars}; "
          f"cost: taker {args.taker_bps}bps + slip {args.slip_bps}bps")

    # Step 4: read N_trials FIRST — it must equal the config_space enumeration.
    expected = _grid_size(strategy.config_space())
    pbo_str = f"{report.pbo:.3f}" if report.pbo is not None else "N/A"
    print("\n" + "=" * 60)
    print(f"N_trials   = {report.n_trials}   (expected {expected} from config_space)"
          f"  {'OK' if report.n_trials == expected else 'MISMATCH!'}")
    print(f"CPCV paths = {len(report.cpcv_sharpe_distribution)}")
    print(f"SR_median  = {report.sharpe_median:.4f}  (per-bar OOS)")
    print(f"PSR        = {report.psr:.4f}")
    print(f"DSR        = {report.deflated_sharpe_ratio:.4f}  (promote > {config.dsr_promote_threshold})")
    print(f"PBO        = {pbo_str}  (promote < {config.pbo_promote_threshold})")
    print(f"VERDICT    = {report.verdict}  —  {report.verdict_reason}")
    print("=" * 60)


if __name__ == "__main__":
    main()
