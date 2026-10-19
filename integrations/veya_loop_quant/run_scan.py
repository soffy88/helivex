"""批量扫描：现有 6 个策略 × Veya Loop BO（研究态，检查 Veya Loop 实用性）。

每个策略：train 区间 BO（n_init+n_iter 次回测）→ best 在 test 区间复评 → risk_gate
（默认以 test 为准）→ JSONL 审计。输出汇总表。

用法：
    python integrations/veya_loop_quant/run_scan.py [--quick]
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parents[1]
sys.path.insert(0, str(_HERE))
sys.path.insert(0, str(_REPO))  # helivex 包（helivex.scripts.run_backtest_v51）

from helivex_adapter.audit_log import log_optimize
from helivex_adapter.optimize_loop import optimize_strategy_params
from helivex_adapter.risk_gate import RiskLimits

DATA = Path("data")

# 策略 → (timeframe, parquet, train, test, search_space, fixed)
# 窗口：4h/1d 用 2024→2026 全史（4H 表）；1h/5m 用 2026-04 起（5m 表聚合）
SCAN = [
    dict(
        strategy_id="trend_dual", timeframe="4h",
        parquet="scan_btc_4h.parquet",
        train=("2024-01-01", "2026-04-01"), test=("2026-04-01", "2026-08-01"),
        search_space={"st_mult": (2.0, 4.0), "ema_fast": (10.0, 30.0),
                      "adx_threshold": (20.0, 30.0)},
        fixed={},
        cpcv=dict(n_groups=8, n_test_groups=2, embargo_bars=4),
    ),
    dict(
        strategy_id="trend_follower_port", timeframe="1d",
        parquet="scan_btc_1d.parquet",
        train=("2024-01-01", "2026-04-01"), test=("2026-04-01", "2026-08-01"),
        search_space={"donchian_n": (15.0, 30.0), "chandelier_mult": (2.0, 4.0),
                      "adx_entry": (15.0, 25.0)},
        fixed={},
        cpcv=dict(n_groups=6, n_test_groups=1, embargo_bars=4),
    ),
    dict(
        strategy_id="vwap_mr_1h", timeframe="1h",
        parquet="scan_btc_1h.parquet",
        train=("2026-04-15", "2026-06-15"), test=("2026-06-15", "2026-08-01"),
        search_space={"z_threshold": (1.5, 3.0), "bb_std": (1.5, 2.5),
                      "hold_bars": (4.0, 8.0)},
        fixed={"std_window": 24},
        cpcv=dict(n_groups=8, n_test_groups=2, embargo_bars=24),
    ),
    dict(
        strategy_id="futures_signal_port", timeframe="1h",
        parquet="scan_btc_1h.parquet",
        train=("2026-04-15", "2026-06-15"), test=("2026-06-15", "2026-08-01"),
        search_space={"breakout_period": (15.0, 30.0), "vol_surge_mult": (1.2, 2.0),
                      "rsi_period": (10.0, 20.0)},
        fixed={},
        cpcv=dict(n_groups=8, n_test_groups=2, embargo_bars=24),
    ),
    dict(
        strategy_id="scalper_v2_port", timeframe="5m",
        parquet="scan_btc_5m.parquet",
        train=("2026-05-01", "2026-06-15"), test=("2026-06-15", "2026-08-01"),
        search_space={"adx_enter": (18.0, 26.0), "bb_k": (1.5, 2.5),
                      "atr_trailing_mult": (1.2, 2.0)},
        fixed={},
        cpcv=dict(n_groups=6, n_test_groups=1, embargo_bars=12),
    ),
    dict(
        strategy_id="scalp_5m", timeframe="5m",
        parquet="scan_btc_5m.parquet",
        train=("2026-05-01", "2026-06-15"), test=("2026-06-15", "2026-08-01"),
        search_space={"vwap_window": (8.0, 20.0), "z_threshold": (1.5, 3.0),
                      "hold_bars": (4.0, 10.0)},
        fixed={},
        cpcv=dict(n_groups=6, n_test_groups=1, embargo_bars=12),
    ),
]

LIMITS = RiskLimits(min_sharpe=0.3, max_drawdown=0.25, min_trades=20, max_turnover=50.0)
N_INIT, N_ITER = 3, 5


def main() -> int:
    quick = "--quick" in sys.argv
    audit_dir = Path("integrations/veya_loop_quant/audit")
    print(f"{'strategy':22s} {'tf':3s} {'n_evals':>7s} {'t(s)':>6s} | "
          f"{'train SR':>8s} {'MDD':>6s} {'ret%':>7s} {'ntr':>5s} | "
          f"{'test SR':>8s} {'MDD':>6s} {'ret%':>7s} {'ntr':>5s} | accept")
    for spec in SCAN:
        sid = spec["strategy_id"]
        t0 = time.time()
        grid = {
            k: [round(float(v), 2) for v in (lo, (lo + hi) / 2, hi)]
            for k, (lo, hi) in spec["search_space"].items()
        }
        try:
            res = optimize_strategy_params(
                strategy_id=sid,
                symbols=["BTC-USDT-SWAP"],
                start=spec["train"][0], end=spec["train"][1],
                test_window=spec["test"],
                search_space=spec["search_space"],
                fixed_params=spec["fixed"],
                n_init=N_INIT, n_iter=N_ITER, seed=0,
                limits=LIMITS,
                timeframe=spec["timeframe"],
                config_grid=grid,
                backtest_extra={"parquet_path": str(DATA / spec["parquet"]),
                                "cpcv": spec["cpcv"]},
            )
            m, tm = res.best_metrics, res.test_metrics
            meta = {"scan": True, "strategy": sid, "timeframe": spec["timeframe"],
                    "train": list(spec["train"]), "test": list(spec["test"]),
                    "search_space": spec["search_space"], "limits": LIMITS.__dict__}
            log_optimize(str(audit_dir / f"scan_{sid}.jsonl"), res, meta)
            print(f"{sid:22s} {spec['timeframe']:3s} {len(res.history):7d} {time.time()-t0:6.1f} | "
                  f"{m.sharpe:8.3f} {m.max_drawdown:6.3f} {m.total_return*100:7.2f} {m.n_trades:5d} | "
                  f"{tm.sharpe if tm else float('nan'):8.3f} "
                  f"{tm.max_drawdown if tm else float('nan'):6.3f} "
                  f"{(tm.total_return*100) if tm else float('nan'):7.2f} "
                  f"{tm.n_trades if tm else -1:5d} | "
                  f"{'ACCEPT' if res.accepted else 'REJECT'} {res.gate_reason}")
            print(f"    best: { {k: round(v, 3) for k, v in res.best_params.items()} }")
        except Exception as exc:  # 单策略失败不中断扫描
            print(f"{sid:22s} {spec['timeframe']:3s} ERROR {type(exc).__name__}: {str(exc)[:120]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
