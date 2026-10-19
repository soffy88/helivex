"""契约测试（CC 规格 §9.1）：mock Metrics → utility 单调、gate 拒收 MDD/交易过少。"""
from __future__ import annotations

import numpy as np
import pytest

from helivex_adapter.discovery_types import BacktestMetrics
from helivex_adapter.metrics import metrics_from_equity
from helivex_adapter.risk_gate import RiskLimits, risk_gate, utility


def _m(**kw) -> BacktestMetrics:
    base = dict(
        total_return=0.2, sharpe=1.0, max_drawdown=0.1,
        n_trades=100, turnover=12.0, cost_drag=0.01,
    )
    base.update(kw)
    return BacktestMetrics(**base)


# ── utility ──────────────────────────────────────────────────────────────

def test_utility_monotonic_in_sharpe():
    u1 = utility(_m(sharpe=0.5))
    u2 = utility(_m(sharpe=1.5))
    assert u2 > u1


def test_utility_penalizes_mdd_and_turnover():
    assert utility(_m(max_drawdown=0.05)) > utility(_m(max_drawdown=0.20))
    assert utility(_m(turnover=5.0)) > utility(_m(turnover=40.0))


def test_utility_nan_safe():
    m = _m(turnover=float("nan"))
    assert utility(m) == utility(_m(turnover=0.0))


def test_utility_formula():
    m = _m(sharpe=1.0, max_drawdown=0.1, turnover=10.0)
    assert utility(m) == pytest.approx(1.0 - 0.1 - 0.05 * 10.0)


# ── risk_gate ────────────────────────────────────────────────────────────

def test_gate_accepts_good_metrics():
    ok, reason = risk_gate(_m(), RiskLimits())
    assert ok and reason == "ok"


def test_gate_rejects_high_mdd():
    ok, reason = risk_gate(_m(max_drawdown=0.30), RiskLimits(max_drawdown=0.25))
    assert not ok and "mdd" in reason


def test_gate_rejects_low_sharpe():
    ok, reason = risk_gate(_m(sharpe=0.2), RiskLimits(min_sharpe=0.5))
    assert not ok and "sharpe" in reason


def test_gate_rejects_few_trades():
    ok, reason = risk_gate(_m(n_trades=5), RiskLimits(min_trades=30))
    assert not ok and "n_trades" in reason


def test_gate_skips_unknown_trades():
    ok, _ = risk_gate(_m(n_trades=-1), RiskLimits(min_trades=30))
    assert ok  # n_trades=-1（兜底未知）不阻断


def test_gate_rejects_high_turnover():
    ok, reason = risk_gate(_m(turnover=99.0), RiskLimits(max_turnover=50.0))
    assert not ok and "turnover" in reason


def test_gate_skips_nan_turnover():
    ok, _ = risk_gate(_m(turnover=float("nan")), RiskLimits(max_turnover=50.0))
    assert ok


# ── metrics_from_equity 兜底 ─────────────────────────────────────────────

def test_metrics_from_equity_flat():
    m = metrics_from_equity(np.ones(100))
    assert m.sharpe == pytest.approx(0.0, abs=1e-9)
    assert m.max_drawdown == pytest.approx(0.0, abs=1e-9)
    assert m.total_return == pytest.approx(0.0, abs=1e-9)
    assert m.n_trades == -1


def test_metrics_from_equity_trend():
    eq = np.linspace(1.0, 1.5, 253)
    m = metrics_from_equity(eq)
    assert m.total_return == pytest.approx(0.5)
    assert m.sharpe > 0
    assert m.max_drawdown >= 0.0


def test_metrics_from_equity_drawdown_detected():
    eq = np.array([1.0, 1.2, 0.9, 1.1])
    m = metrics_from_equity(eq)
    assert m.max_drawdown == pytest.approx(0.25)  # 1.2 → 0.9


def test_metrics_from_equity_too_short():
    m = metrics_from_equity(np.array([1.0, 1.1]))
    assert m.max_drawdown == 1.0  # 哨兵：样本不足
