"""效用与风控门禁（CC 规格 §3.4）。

utility = w_sharpe·Sharpe − w_mdd·MDD − w_turn·turnover（越大越好）。
risk_gate 按 RiskLimits 逐项检查，返回 (通过?, 原因)。
"""
from __future__ import annotations

from dataclasses import dataclass

from helivex_adapter.discovery_types import BacktestMetrics


@dataclass
class RiskLimits:
    min_sharpe: float = 0.5
    max_drawdown: float = 0.25      # 25%
    min_trades: int = 30
    max_turnover: float = 50.0      # 年化 one-way 换手（倍/年）


def utility(
    m: BacktestMetrics, *, w_sharpe: float = 1.0, w_mdd: float = 1.0, w_turn: float = 0.05
) -> float:
    """越大越好。回撤、换手作惩罚。NaN 字段按 0 处理（NaN-safe）。"""
    turn = m.turnover if m.turnover == m.turnover else 0.0
    return w_sharpe * m.sharpe - w_mdd * m.max_drawdown - w_turn * turn


def risk_gate(m: BacktestMetrics, lim: RiskLimits) -> tuple[bool, str]:
    """门禁：交易数 / 回撤 / Sharpe / 换手。全部通过才 accept。"""
    if m.n_trades >= 0 and m.n_trades < lim.min_trades:
        return False, f"n_trades {m.n_trades} < {lim.min_trades}"
    if m.max_drawdown > lim.max_drawdown:
        return False, f"mdd {m.max_drawdown:.3f} > {lim.max_drawdown}"
    if m.sharpe < lim.min_sharpe:
        return False, f"sharpe {m.sharpe:.3f} < {lim.min_sharpe}"
    if m.turnover == m.turnover and m.turnover > lim.max_turnover:
        return False, f"turnover {m.turnover:.2f} > {lim.max_turnover}"
    return True, "ok"
