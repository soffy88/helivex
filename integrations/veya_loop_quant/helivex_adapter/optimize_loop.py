"""优化闭环：Veya Loop BO 搜参 + 效用 + train/test 门禁（CC 规格 §4 + §6）。

- 优化只发生在训练区间 [start, end)（防过拟合硬性建议 §6.1）；
- 提供 `test_window` 时，best 在测试区间再评一次，`accepted` 以测试集门禁为准
  （§6.2 / §6.5，`gate_on="test"`）；
- 禁止用测试集调 search_space 边界（§6.4）。
"""
from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

import numpy as np

from veya_loop.oprim._bayes_opt_plan import bayesian_optimize

from helivex_adapter.backtest_bridge import run_backtest
from helivex_adapter.discovery_types import (
    BacktestMetrics,
    BacktestRequest,
    OptimizeResult,
)
from helivex_adapter.risk_gate import RiskLimits, risk_gate, utility


def _bo_result(bo: Dict[str, Any], key: str):
    """兼容 veya_loop 返回 dict 与（未来）属性对象两种形态。"""
    if isinstance(bo, dict):
        return bo[key]
    return getattr(bo, key)


def optimize_strategy_params(
    *,
    strategy_id: str,
    symbols: Sequence[str],
    start: str,
    end: str,
    search_space: Mapping[str, Tuple[float, float]],
    fixed_params: Optional[Dict[str, float]] = None,
    n_init: int = 4,
    n_iter: int = 16,
    seed: int = 0,
    limits: Optional[RiskLimits] = None,
    timeframe: str = "1h",
    capital: float = 1_000_000.0,
    test_window: Optional[Tuple[str, str]] = None,
    gate_on: str = "auto",          # "auto" → test_window 时按 test，否则按 train
    config_grid: Optional[Dict[str, Sequence[float]]] = None,
    backtest_extra: Optional[Dict[str, Any]] = None,
) -> OptimizeResult:
    """在 [start, end) 上用 Veya Loop BO 最大化 utility(backtest(params))。

    search_space: {param_name: (low, high)} 连续盒约束（SignalConfig 字段名）。
    test_window: (start_test, end_test)；提供后 best 在测试区间复评，门禁默认以测试集为准。
    """
    limits = limits or RiskLimits()
    fixed_params = dict(fixed_params or {})
    keys = list(search_space.keys())
    bounds = [search_space[k] for k in keys]
    extra = dict(backtest_extra or {})
    if config_grid is not None:
        extra.setdefault("config_grid", config_grid)
    history: List[Dict[str, Any]] = []

    def fn(x: np.ndarray) -> float:
        params = {**fixed_params, **{k: float(v) for k, v in zip(keys, x)}}
        req = BacktestRequest(
            strategy_id=strategy_id,
            params=params,
            symbols=list(symbols),
            start=start,
            end=end,
            timeframe=timeframe,
            capital=capital,
            extra=dict(extra),
        )
        m = run_backtest(req)
        u = utility(m)
        # 硬惩罚：交易数不足（死区/不交易点）直接压低效用，避免 BO 收敛到
        # “不交易 = utility 0 > 亏损”的平凡解（扫描实测发现的退化路径）。
        if m.n_trades >= 0 and m.n_trades < limits.min_trades:
            u -= 10.0
        history.append({"params": params, "utility": u, "metrics": m})
        return u

    bo = bayesian_optimize(fn, bounds=bounds, n_init=n_init, n_iter=n_iter,
                           seed=seed, minimize=False)  # 最大化 utility

    best_x = _bo_result(bo, "best_x")
    best_params = {**fixed_params, **{k: float(v) for k, v in zip(keys, best_x)}}
    best_m = run_backtest(
        BacktestRequest(
            strategy_id=strategy_id,
            params=best_params,
            symbols=list(symbols),
            start=start,
            end=end,
            timeframe=timeframe,
            capital=capital,
            extra=dict(extra),
        )
    )

    test_m: Optional[BacktestMetrics] = None
    if test_window is not None:
        test_m = run_backtest(
            BacktestRequest(
                strategy_id=strategy_id,
                params=best_params,
                symbols=list(symbols),
                start=test_window[0],
                end=test_window[1],
                timeframe=timeframe,
                capital=capital,
                extra=dict(extra),
            )
        )

    # 门禁：默认 test_window 提供后按测试集（gate_on="auto"/"test"），否则按训练集。
    gate_metrics = test_m if (gate_on != "train" and test_m is not None) else best_m
    ok, reason = risk_gate(gate_metrics, limits)

    return OptimizeResult(
        best_params=best_params,
        best_metrics=best_m,
        best_utility=float(_bo_result(bo, "best_y")),
        history=history,
        accepted=ok,
        gate_reason=reason,
        test_metrics=test_m,
    )
