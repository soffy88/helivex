"""Smoke：假 Engine（随机 equity / 确定性假回测）→ BO 闭环 + 门禁 + JSONL 审计（CC 规格 §9.2）。"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

import helivex_adapter.optimize_loop as ol
from helivex_adapter.discovery_types import BacktestMetrics, BacktestRequest
from helivex_adapter.metrics import metrics_from_equity
from helivex_adapter.optimize_loop import optimize_strategy_params
from helivex_adapter.risk_gate import RiskLimits


# ── 假引擎：随机 equity + 参数相关信号 ────────────────────────────────────

def _fake_random_equity(req: BacktestRequest, rng: np.random.Generator) -> BacktestMetrics:
    """随机游走 equity：Sharpe 与 param0 到最优点的距离负相关（给 BO 一个可寻信号）。"""
    lookback = req.params.get("mom_donchian_lookback", 30.0)
    kelly = req.params.get("kelly_fraction", 0.25)
    opt = (35.0, 0.3)  # 最优点
    dist = abs(lookback - opt[0]) / 60.0 + abs(kelly - opt[1]) / 0.5
    n = 300
    rets = rng.normal(loc=0.001 * (1.2 - dist), scale=0.02, size=n)
    equity = np.cumprod(1 + rets)
    m = metrics_from_equity(equity)
    m.n_trades = max(10, int(150 * (1 - dist)))
    m.turnover = 5.0 + 20.0 * (1 - dist)
    return m


@pytest.fixture()
def fake_engine(monkeypatch):
    rng = np.random.default_rng(42)

    def fake(req: BacktestRequest) -> BacktestMetrics:
        return _fake_random_equity(req, rng)

    monkeypatch.setattr(ol, "run_backtest", fake)


def test_smoke_bo_with_fake_engine(tmp_path, fake_engine):
    res = optimize_strategy_params(
        strategy_id="directional",
        symbols=["BTC-USDT-SWAP"],
        start="2026-05-01",
        end="2026-06-01",
        search_space={
            "mom_donchian_lookback": (10.0, 60.0),
            "kelly_fraction": (0.1, 0.5),
        },
        n_init=4,
        n_iter=8,
        seed=0,
        limits=RiskLimits(min_sharpe=-10.0, max_drawdown=10.0, min_trades=0),  # 宽松，保证 accept
    )
    assert len(res.history) == 12  # n_init + n_iter
    assert set(res.best_params) == {"mom_donchian_lookback", "kelly_fraction"}
    assert res.best_metrics.n_trades > 0
    assert res.accepted is True
    assert res.gate_reason == "ok"
    # BO 应逼近最优点（35, 0.3）附近
    assert abs(res.best_params["mom_donchian_lookback"] - 35.0) < 30.0
    assert res.best_utility == pytest.approx(max(h["utility"] for h in res.history))


def test_smoke_test_window_gate(tmp_path, fake_engine):
    """test_window 提供后：gate 以测试集为准；测试集 Sharpe 差 → 拒收。"""
    from helivex_adapter import risk_gate as rg

    original = ol.run_backtest

    def fake_with_weak_test(req: BacktestRequest) -> BacktestMetrics:
        m = original(req)
        if req.start >= "2026-07-01":  # 测试区间：把 Sharpe 压到门禁之下
            m.sharpe = -1.0
            m.max_drawdown = 0.9
        return m

    ol.run_backtest = fake_with_weak_test
    try:
        res = optimize_strategy_params(
            strategy_id="directional",
            symbols=["BTC-USDT-SWAP"],
            start="2026-05-01",
            end="2026-06-01",
            test_window=("2026-07-01", "2026-08-01"),
            search_space={"kelly_fraction": (0.1, 0.5)},
            n_init=3,
            n_iter=3,
            seed=1,
            limits=RiskLimits(min_sharpe=0.0, max_drawdown=1.0, min_trades=0),
        )
        assert res.test_metrics is not None
        assert res.test_metrics.sharpe == -1.0
        assert res.accepted is False
        assert "sharpe" in res.gate_reason
    finally:
        ol.run_backtest = original


def test_smoke_audit_jsonl(tmp_path, fake_engine):
    audit_path = tmp_path / "audit" / "optimize.jsonl"
    res = optimize_strategy_params(
        strategy_id="directional",
        symbols=["BTC-USDT-SWAP"],
        start="2026-05-01",
        end="2026-06-01",
        search_space={"kelly_fraction": (0.1, 0.5)},
        n_init=2,
        n_iter=2,
        seed=2,
        limits=RiskLimits(min_sharpe=-10.0, max_drawdown=10.0, min_trades=0),
    )
    from helivex_adapter.audit_log import log_optimize

    log_optimize(str(audit_path), res, {"src": "test", "train": ["2026-05-01", "2026-06-01"]})
    lines = audit_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert rec["accepted"] is True
    assert rec["gate_reason"] == "ok"
    assert set(rec["metrics"]) >= {"sharpe", "mdd", "ret", "n_trades"}
    assert rec["meta"]["src"] == "test"
    assert rec["n_evals"] == 4
    assert "ts" in rec and "best_params" in rec


def test_smoke_bo_rejects_overfit_none(tmp_path, fake_engine):
    """极端 MDD 门禁（0.0）→ 必然拒收，返回 accepted=False 而非抛错。"""
    res = optimize_strategy_params(
        strategy_id="directional",
        symbols=["BTC-USDT-SWAP"],
        start="2026-05-01",
        end="2026-06-01",
        search_space={"kelly_fraction": (0.1, 0.5)},
        n_init=2,
        n_iter=2,
        seed=3,
        limits=RiskLimits(min_sharpe=99.0, max_drawdown=0.0, min_trades=10**9),
    )
    assert res.accepted is False
    assert res.gate_reason != "ok"
