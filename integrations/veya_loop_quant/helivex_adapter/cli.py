"""CLI：从 YAML 配置跑一次「真实 Helivex」BO 优化 + JSONL 审计。

用法：
    python -m helivex_adapter.cli --config integrations/veya_loop_quant/configs/example_search_space.yaml
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import yaml

from helivex_adapter.audit_log import log_optimize
from helivex_adapter.optimize_loop import optimize_strategy_params
from helivex_adapter.risk_gate import RiskLimits


def _load(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def main() -> int:
    p = argparse.ArgumentParser(description="Veya Loop BO × Helivex 回测（研究态）")
    p.add_argument("--config", required=True, help="search-space YAML")
    p.add_argument("--audit", default="integrations/veya_loop_quant/audit/optimize.jsonl")
    p.add_argument("--seed", type=int, default=None)
    p.add_argument("--n-iter", type=int, default=None, help="覆写 YAML 的 n_iter")
    args = p.parse_args()

    cfg = _load(args.config)
    limits = RiskLimits(**cfg.get("limits", {}))
    test_window = cfg.get("test_window")
    if isinstance(test_window, list):
        test_window = tuple(test_window)

    result = optimize_strategy_params(
        strategy_id=cfg["strategy_id"],
        symbols=cfg["symbols"],
        start=cfg["start"],
        end=cfg["end"],
        search_space=cfg["search_space"],
        fixed_params=cfg.get("fixed_params"),
        n_init=int(cfg.get("n_init", 4)),
        n_iter=int(args.n_iter or cfg.get("n_iter", 16)),
        seed=int(args.seed if args.seed is not None else cfg.get("seed", 0)),
        limits=limits,
        timeframe=cfg.get("timeframe", "1h"),
        capital=float(cfg.get("capital", 1_000_000.0)),
        test_window=test_window,
        gate_on=str(cfg.get("gate_on", "auto")),
        backtest_extra={
            k: v for k, v in cfg.get("backtest", {}).items()
        },
    )

    meta = {
        "config": str(Path(args.config).resolve()),
        "strategy_id": cfg["strategy_id"],
        "symbols": cfg["symbols"],
        "train": [cfg["start"], cfg["end"]],
        "test": list(test_window) if test_window else None,
        "search_space": cfg["search_space"],
        "limits": cfg.get("limits", {}),
    }
    log_optimize(args.audit, result, meta)

    print(json.dumps({
        "best_params": result.best_params,
        "train": {
            "sharpe": result.best_metrics.sharpe,
            "mdd": result.best_metrics.max_drawdown,
            "ret": result.best_metrics.total_return,
            "n_trades": result.best_metrics.n_trades,
            "turnover": result.best_metrics.turnover,
            "verdict": result.best_metrics.raw.get("verdict"),
        },
        "test": None if result.test_metrics is None else {
            "sharpe": result.test_metrics.sharpe,
            "mdd": result.test_metrics.max_drawdown,
            "ret": result.test_metrics.total_return,
            "n_trades": result.test_metrics.n_trades,
            "turnover": result.test_metrics.turnover,
        },
        "best_utility": result.best_utility,
        "accepted": result.accepted,
        "gate_reason": result.gate_reason,
        "n_evals": len(result.history),
        "audit": args.audit,
    }, ensure_ascii=False, indent=2))
    return 0 if result.accepted else 1


if __name__ == "__main__":
    sys.exit(main())
