"""continuous_counterfactual — 完整版反事实推演（spec §5 可延后项，现落地）。

轻量版（run_cost_counterfactual.py）是「枚举重跑」：每个成本点都要引擎回测。
完整版：先拟合 Veya Loop `HybridSCM`（参数/成本/波动 → 指标的因果结构），之后
任意干预组合无需重跑引擎即可 L3 反事实推演（锚定事实噪声），并可上
`continuous_plan_with_hybrid_bo` 做连续规划。

诚实边界：
- SCM 是近似模型（条件仿射机制），外推区（无数据处）不可靠；
- 每次推演都提供「引擎重跑交叉验证」入口（spec 原则：引擎仍是真相）；
- 本模块只做决策支持，不替代 run_backtest。
"""
from __future__ import annotations

import json
import math
import random
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Sequence, Tuple

import networkx as nx
import numpy as np

from veya_loop.oprim._hybrid_scm import NodeSpec, build_hybrid_scm
from veya_loop.oprim._bayes_opt_plan import continuous_plan_with_hybrid_bo

from helivex_adapter.backtest_bridge import _resolve_bars, run_backtest
from helivex_adapter.discovery_types import BacktestRequest

SYMBOLS = ["BTC-USDT-SWAP", "ETH-USDT-SWAP", "SOL-USDT-SWAP"]
DATA_DIR = Path("data")


# ---------------------------------------------------------------------------
# 采样：参数 × 成本 × 标的 × 窗口 → 引擎回测指标（真相数据）
# ---------------------------------------------------------------------------

def _window_vol(symbol: str, tf: str, start: str, end: str, parquet: str) -> float:
    req = BacktestRequest(strategy_id="x", params={}, symbols=[symbol],
                          start=start, end=end, timeframe=tf,
                          extra={"parquet_path": parquet})
    bars = _resolve_bars(req, symbol=symbol)
    rets = np.array([b.close_return for b in bars if b.close_return == b.close_return])
    if len(rets) < 3:
        return 0.0
    return float(np.std(rets) * math.sqrt(252.0))  # 年化 realized vol


def sample_dataset(
    *,
    strategy_id: str,
    timeframe: str,
    parquet_map: Dict[str, str],
    windows: Sequence[Tuple[str, str]],
    param_grid: Dict[str, Sequence[float]],
    cost_grid: Sequence[float],
    n_samples: int = 60,
    seed: int = 0,
    symbols: Sequence[str] | None = None,
    full_cross: bool = False,
    extra: Dict[str, Any] | None = None,
) -> Dict[str, np.ndarray]:
    """随机子采样或全交叉采样参数×成本×窗口，跑引擎，返回节点观测 dict（真相）。

    symbols=None → 随机单标的（vol 有标的间变化，但样本噪声大）；
    symbols 给出 → 组合回测（噪声小，vol = 各标的窗口 vol 均值）。
    full_cross=True → 参数×成本×窗口确定性全交叉（推荐：成本/窗口效应不被
    参数混杂淹没，随机子采样在目标效应 < 混杂噪声时学不到效应）。

    节点：cost_bps, vol, 各搜索参数, n_trades, turnover, cost_drag,
          gross_ret(≈ret+cost), net_ret, sharpe, mdd
    """
    rng = random.Random(seed)
    rows: Dict[str, List[float]] = {k: [] for k in
        ["cost_bps", "vol", *param_grid.keys(), "n_trades", "turnover",
         "cost_drag", "gross_ret", "net_ret", "sharpe", "mdd"]}
    cost_model_base = dict(extra or {}).get("cost_model", {})
    if full_cross:
        import itertools
        keys = list(param_grid)
        combos = list(itertools.product(
            *(param_grid[k] for k in keys), cost_grid, windows))
    else:
        combos = None
    for i in range(n_samples if combos is None else len(combos)):
        if combos is not None:
            vals = combos[i]
            pvals, cost, win = vals[:-2], vals[-2], vals[-1]
            params = dict(zip(keys, pvals))
        else:
            params = {k: rng.choice(v) for k, v in param_grid.items()}
            cost = rng.choice(cost_grid)
            win = rng.choice(windows)
        syms = list(symbols) if symbols else [rng.choice(SYMBOLS)]
        vol = float(np.mean([
            _window_vol(s, timeframe, win[0], win[1], parquet_map[s]) for s in syms
        ]))
        m = run_backtest(BacktestRequest(
            strategy_id=strategy_id, params=params, symbols=syms,
            start=win[0], end=win[1], timeframe=timeframe,
            extra={
                "parquet_path": {s: parquet_map[s] for s in syms},
                "cpcv": dict(n_groups=6, n_test_groups=1, embargo_bars=4),
                "cost_model": {**cost_model_base,
                               "taker_fee_bps": cost, "fixed_slippage_bps": 0.0},
            },
        ))
        rows["cost_bps"].append(float(cost))
        rows["vol"].append(vol)
        for k, v in params.items():
            rows[k].append(float(v))
        rows["n_trades"].append(float(m.n_trades))
        rows["turnover"].append(float(m.turnover))
        rows["cost_drag"].append(float(m.cost_drag))
        rows["net_ret"].append(float(m.total_return))
        rows["gross_ret"].append(float(m.total_return + m.cost_drag))
        rows["sharpe"].append(float(m.sharpe))
        rows["mdd"].append(float(m.max_drawdown))
    return {k: np.asarray(v, dtype=float).reshape(-1, 1) for k, v in rows.items()}


# ---------------------------------------------------------------------------
# SCM 构建
# ---------------------------------------------------------------------------

def build_scm(data: Dict[str, np.ndarray], param_names: Sequence[str],
              epochs: int = 0) -> Any:
    """DAG：参数/成本/波动 → 交易行为 → 收益/风险 → Sharpe。

    epochs=0 → 线性条件仿射（小样本稳）；全连续 diagonal 机制。
    """
    dag = nx.DiGraph()
    inputs = ["vol", "cost_bps", *param_names]
    nodes = [*inputs, "n_trades", "cost_drag", "gross_ret", "net_ret", "mdd", "sharpe"]
    dag.add_nodes_from(nodes)
    # 因果边（输入节点无父）
    for p in param_names:
        dag.add_edge(p, "n_trades")
        dag.add_edge(p, "gross_ret")
        dag.add_edge(p, "mdd")
    dag.add_edge("vol", "n_trades")
    dag.add_edge("vol", "gross_ret")
    dag.add_edge("vol", "mdd")
    dag.add_edge("vol", "sharpe")
    dag.add_edge("n_trades", "cost_drag")
    dag.add_edge("cost_bps", "cost_drag")
    dag.add_edge("gross_ret", "net_ret")
    dag.add_edge("cost_drag", "net_ret")
    dag.add_edge("net_ret", "sharpe")
    dag.add_edge("mdd", "sharpe")

    specs = {n: NodeSpec(name=n, kind="continuous", dim=1,
                         continuous_mech="diagonal") for n in nodes}
    scm = build_hybrid_scm(dag, specs)
    scm.fit(data, epochs=epochs, shrinkage="lw", seed=0)
    return scm


# ---------------------------------------------------------------------------
# 案例：L3 推演 + 连续规划
# ---------------------------------------------------------------------------

def counterfactual_curve(
    scm: Any, factual: Dict[str, np.ndarray], query: str,
    intervene: str, values: Sequence[float],
    clamp: Tuple[float, float] | None = None,
) -> List[Dict[str, float]]:
    """锚定事实噪声，扫干预值 → query 的 L3 推演曲线。

    clamp：推演值钳制范围（线性 SCM 外推可能出荒谬值，如 MDD<0）。
    """
    out = []
    for v in values:
        y = scm.l3_counterfactual(factual, {intervene: [float(v)]}, query)
        val = float(np.asarray(y).ravel()[0])
        if clamp is not None:
            val = max(clamp[0], min(clamp[1], val))
        out.append({"intervene": intervene, "value": float(v),
                    "query": query, "l3": val})
    return out


def breakeven_cost(scm: Any, factual: Dict[str, np.ndarray],
                   lo: float = 1.0, hi: float = 15.0,
                   n: int = 24) -> float | None:
    """扫 cost_bps 找 sharpe 过零的成本（净正门槛）。"""
    curve = counterfactual_curve(scm, factual, "sharpe", "cost_bps",
                                 np.linspace(lo, hi, n).tolist())
    for a, b in zip(curve, curve[1:]):
        if a["l3"] >= 0 >= b["l3"] or a["l3"] <= 0 <= b["l3"]:
            if a["l3"] == b["l3"]:
                return float(a["value"])
            t = (0 - a["l3"]) / (b["l3"] - a["l3"])
            return float(a["value"] + t * (b["value"] - a["value"]))
    return None


def plan_max_sharpe(scm: Any, factual: Dict[str, np.ndarray],
                    bounds_map: Dict[str, Tuple[float, float]],
                    n_init: int = 4, n_iter: int = 10, seed: int = 0) -> Dict[str, Any]:
    """continuous_plan_with_hybrid_bo：在 SCM 上找使 sharpe 最大的干预组合。"""
    return continuous_plan_with_hybrid_bo(
        scm, factual, "sharpe", bounds_map,
        n_init=n_init, n_iter=n_iter, seed=seed, minimize=False)


def save_jsonl(path: str, recs: List[Dict[str, Any]]) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        for r in recs:
            f.write(json.dumps(r, ensure_ascii=False, default=str) + "\n")
