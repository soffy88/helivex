"""指标兜底：由 equity 曲线计算 Sharpe / MDD / 总收益（CC 规格 §3.3）。

Helivex 引擎已提供稳定 Sharpe（ValidationReport.sharpe_median）与每路径 MDD，
本模块仅作为「引擎缺字段 / 假引擎 / 原始曲线」场景的兜底，不替代引擎口径。
"""
from __future__ import annotations

import numpy as np

from helivex_adapter.discovery_types import BacktestMetrics


def metrics_from_equity(
    equity: np.ndarray, *, periods_per_year: float = 252.0
) -> BacktestMetrics:
    """equity 曲线（初始值任意，>0）→ BacktestMetrics。

    约定：
    - sharpe = mean(ret)/std(ret) * sqrt(periods_per_year)，ret 为逐 bar 简单收益；
    - max_drawdown 为正数（0.15 = 15%）；
    - n_trades = -1 表示未知（risk_gate 对 n_trades<0 跳过交易数检查）。
    """
    equity = np.asarray(equity, dtype=float)
    if equity.size < 3:
        return BacktestMetrics(0.0, 0.0, 1.0, -1, equity=equity)
    rets = np.diff(equity) / np.clip(equity[:-1], 1e-12, None)
    mu, sigma = float(np.mean(rets)), float(np.std(rets) + 1e-12)
    sharpe = mu / sigma * np.sqrt(periods_per_year)
    peak = np.maximum.accumulate(equity)
    dd = (peak - equity) / np.clip(peak, 1e-12, None)
    mdd = float(np.max(dd))
    total = float(equity[-1] / equity[0] - 1.0)
    return BacktestMetrics(
        total_return=total,
        sharpe=float(sharpe),
        max_drawdown=mdd,
        n_trades=-1,
        equity=equity,
    )
