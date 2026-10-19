"""backtest_bridge — 唯一允许 import Helivex 内部 API 的文件（CC 规格 §3.2 / §1 DISCOVERY）。

对外只暴露 `run_backtest(req) -> BacktestMetrics`。

Helivex 真相来源（见 docs/integration/HELIVEX_DISCOVERY.md）：
1. `helios_backtest_v51.run_validation`：CPCV(10,2) 验证 → sharpe_median / per_path_max_drawdown
   / DSR / PBO / verdict（引擎的稳定 Sharpe 口径）。
2. `helios_backtest_v51.simulator.simulate`：全窗口逐 bar 撮合 → equity / trades / 成本
   （ValidationReport 不携带全窗口曲线，用它补齐 total_return / n_trades / turnover）。
3. 数据：`run_features` 产物 parquet → `load_bars`（helivex.scripts.run_backtest_v51）。

本文件是「唯一碰 Helivex 内部」处；其余模块只见 BacktestRequest / BacktestMetrics。
"""
from __future__ import annotations

import dataclasses
import math
from pathlib import Path
from typing import Any, Dict, List

import numpy as np
import pandas as pd

from helios_backtest_v51 import (
    BacktestConfig,
    BarRow,
    CostModel,
    ValidationReport,
    run_validation,
)
from helios_backtest_v51.cpcv import CPCVConfig
from helios_backtest_v51.simulator import simulate
from helivex.scripts.run_backtest_v51 import load_bars
from helivex_signal.config import SignalConfig
from helivex_signal.strategy import DEFAULT_CONFIG_GRID, DirectionalStrategy

from helivex_adapter.discovery_types import BacktestMetrics, BacktestRequest
from helivex_adapter.metrics import metrics_from_equity
from helivex_adapter.strategies_port import STRATEGY_REGISTRY

# SignalConfig 中语义为「整数」的字段（BO 产出 float，落库前取整）。
_INT_PARAM_FIELDS = {
    "mom_donchian_lookback",
    "mom_ema_fast",
    "mom_ema_slow",
    "mom_atr_lookback",
    "mom_max_pyramid_tranches",
    "mr_zscore_lookback",
    "mr_atr_lookback",
    "mr_time_stop_bars",
    "edge_ewma_halflife_bars",
    "edge_warmup_trade_count",
}

_MIN_BARS = 60  # CPCV(10,2) 至少需要 ~50 根 bar；低于此直接走兜底，不让引擎抛异常

PERIODS_PER_YEAR = 252.0


# ---------------------------------------------------------------------------
# 策略构造
# ---------------------------------------------------------------------------

def _coerce_params(params: Dict[str, float]) -> Dict[str, Any]:
    out: Dict[str, Any] = dict(params)
    for k, v in out.items():
        if k in _INT_PARAM_FIELDS:
            out[k] = int(round(float(v)))
    return out


def _build_strategy(req: BacktestRequest) -> Any:
    """strategy_id → 引擎侧策略实例。

    - directional / helivex_directional / demo_momentum：helivex_signal.DirectionalStrategy
      （SignalConfig frozen dataclass，dataclasses.replace 覆写）；
    - 其余：strategies_port 注册表（strategies/*.yaml 的移植，参数为类声明的 params）。
    """
    if req.strategy_id in STRATEGY_REGISTRY:
        cls = STRATEGY_REGISTRY[req.strategy_id]
        params = _coerce_port_params(req.params, cls)
        return cls(params=params,
                  position_fraction=float(req.extra.get("position_fraction", 0.5)))

    if req.strategy_id not in ("directional", "helivex_directional", "demo_momentum"):
        raise ValueError(
            f"unknown strategy_id {req.strategy_id!r}; supported: "
            f"directional, {sorted(STRATEGY_REGISTRY)}"
        )
    try:
        base = SignalConfig.for_timeframe(req.timeframe)
    except ValueError as exc:  # 非 1h/15m 的 timeframe
        raise ValueError(f"timeframe {req.timeframe!r} unsupported by SignalConfig") from exc
    params = _coerce_params(req.params)
    unknown = set(params) - {f.name for f in dataclasses.fields(SignalConfig)}
    if unknown:
        raise ValueError(f"unknown SignalConfig params: {sorted(unknown)}")
    cfg = dataclasses.replace(base, **params)
    # config_grid：向引擎声明真实搜索广度（n_trials → DSR 通缩口径）。
    grid = req.extra.get("config_grid") or DEFAULT_CONFIG_GRID
    return DirectionalStrategy(cfg, config_grid=grid)


def _coerce_port_params(params: Dict[str, float], cls: type) -> Dict[str, Any]:
    """移植策略参数：int_fields 取整，未知参数报错。"""
    known = set(cls.params)
    unknown = set(params) - known
    if unknown:
        raise ValueError(f"{cls.name}: unknown params {sorted(unknown)}; known: {sorted(known)}")
    out = {}
    for k, v in params.items():
        out[k] = int(round(float(v))) if k in cls.int_fields else float(v)
    return out


# ---------------------------------------------------------------------------
# 数据解析（只读 parquet，不触碰行情 API）
# ---------------------------------------------------------------------------

def _auto_find_parquet(req: BacktestRequest, data_dir: Path) -> Path | None:
    """按 symbol（归一化）+ timeframe 在 data_dir 里找 parquet。"""
    sym = req.symbols[0].upper().replace(".P", "").replace(".", "_")
    tf = req.timeframe
    wanted = {
        f"{sym}_funded_{tf}.parquet",
        f"{sym}_{tf}.parquet",
        f"{sym}.parquet",
        f"smoke_ftx_funded_{tf}.parquet",  # 仓库自带历史样例
    }
    for cand in sorted(data_dir.glob("*.parquet")):
        if cand.name in wanted:
            return cand
    return None


def load_ohlcv_bars(parquet_path: Path) -> list[Any]:
    """无特征列的 OHLCV parquet（data_tools 产物）→ list[BarRow]。

    volume 塞进 features["volume"]（futures_signal_port 等量信号策略用）；
    其余特征列给中性默认值，与引擎/策略解耦。
    """
    df = pd.read_parquet(parquet_path)
    bars: list[Any] = []
    prev_close = None
    for row in df.itertuples(index=False):
        close = float(row.close)
        log_ret = float(row.log_return) if hasattr(row, "log_return") and \
            row.log_return == row.log_return else (0.0 if prev_close is None
                                                   else math.log(close / prev_close))
        prev_close = close
        bars.append(
            BarRow(
                bar_close_ts=pd.Timestamp(row.bar_close_ts).to_pydatetime(),
                open=float(row.open),
                high=float(row.high),
                low=float(row.low),
                close=close,
                close_return=log_ret,
                funding_rate=float(getattr(row, "funding_rate", 0.0) or 0.0),
                features={
                    "realized_var": 0.0,
                    "tail_index": 0.0,
                    "regime": "neutral",
                    "risk_multiplier": 1.0,
                    "volume": float(getattr(row, "volume", 0.0) or 0.0),
                },
            )
        )
    return bars


def _resolve_bars(req: BacktestRequest, symbol: str | None = None) -> List[Any]:
    """bar 源优先级：extra['bars'] > extra['parquet_path'] > data_dir 自动查找。

    统一按 [start, end) 过滤 bar_close_ts（ISO 字符串 → pandas Timestamp 比较）。
    symbol 参数用于多标的组合（_run_portfolio 逐标的解析）。
    """
    sym = symbol or req.symbols[0]
    if req.extra.get("bars") is not None:
        bars = list(req.extra["bars"])
    else:
        parquet = req.extra.get("parquet_path")
        if isinstance(parquet, dict):
            parquet = parquet.get(sym)
        if parquet is None:
            data_dir = Path(req.extra.get("data_dir", "data"))
            found = _auto_find_parquet(req, data_dir, symbol=sym)
            if found is None:
                raise FileNotFoundError(
                    f"no parquet for {sym}/{req.timeframe} in {data_dir}; "
                    "pass extra['parquet_path'] or run helivex.scripts.run_features first"
                )
            parquet = found
        parquet = Path(parquet).expanduser()
        cols = set(pd.read_parquet(parquet, columns=None).columns)
        if {"realized_var", "regime", "tail_index"} <= cols:
            bars = load_bars(parquet)          # run_features 产物（含特征列）
        else:
            bars = load_ohlcv_bars(parquet)    # data_tools 产物（纯 OHLCV）

    if not bars:
        return []

    # tz-aware UTC 比较（np.datetime64 无法表示 tz，用 pandas Timestamp 对齐）
    start = pd.Timestamp(req.start).tz_localize("UTC")
    end = pd.Timestamp(req.end).tz_localize("UTC")
    keep = [
        b for b in bars
        if start <= pd.Timestamp(b.bar_close_ts) < end
    ]
    return keep


# ---------------------------------------------------------------------------
# 引擎调用 + 指标映射（DISCOVERY §2 字段表）
# ---------------------------------------------------------------------------

def _engine_config(req: BacktestRequest) -> BacktestConfig:
    cpcv_kw = dict(req.extra.get("cpcv") or {})
    cost_kw = dict(req.extra.get("cost_model") or {})
    return BacktestConfig(
        cpcv=CPCVConfig(**cpcv_kw),
        cost_model=CostModel(**cost_kw),
    )


def _map_report_sim(report: ValidationReport, sim, bars: list) -> BacktestMetrics:
    """ValidationReport（CPCV 口径）+ SimResult（全窗口口径）→ BacktestMetrics。"""
    equity = np.asarray(sim.equity_curve, dtype=float)
    n_bars = max(len(sim.per_bar_returns), 1)
    turnover_one_way = float(
        sum(abs(t.new_fraction - t.prev_fraction) for t in sim.trades)
    )
    mdd = (
        float(max(report.per_path_max_drawdown))
        if report.per_path_max_drawdown else 0.0
    )
    total_return = (
        float(equity[-1] / equity[0] - 1.0)
        if equity.size >= 2 and equity[0] > 1e-12 else 0.0
    )
    return BacktestMetrics(
        total_return=total_return,
        sharpe=float(report.sharpe_median),
        max_drawdown=mdd,
        n_trades=len(sim.trades),
        turnover=turnover_one_way * PERIODS_PER_YEAR / n_bars,
        cost_drag=float(sim.total_costs),
        equity=equity,
        raw={
            "verdict": report.verdict,
            "verdict_reason": report.verdict_reason,
            "psr": float(report.psr),
            "deflated_sharpe_ratio": float(report.deflated_sharpe_ratio),
            "pbo": report.pbo,
            "n_trials": int(report.n_trials),
            "per_path_max_drawdown": list(map(float, report.per_path_max_drawdown)),
            "cpcv_sharpe_distribution": list(map(float, report.cpcv_sharpe_distribution)),
            "total_costs": float(sim.total_costs),
            "total_funding": float(sim.total_funding),
            "n_bars": len(bars),
        },
    )


def run_backtest(req: BacktestRequest) -> BacktestMetrics:
    """Helivex → BacktestMetrics（引擎唯一入口）。多标的走组合路径。"""
    if len(req.symbols) > 1:
        return _run_portfolio(req)
    return _run_single(req)


def _run_portfolio(req: BacktestRequest) -> BacktestMetrics:
    """多标的组合回测：每标的独立（validation + simulate）→ 组合净值聚合。

    口径（研究态）：等权净值组合，组合 equity = mean(各标的 equity)（各自从 1.0 起步）；
    sharpe/mdd/ret 由组合净值曲线计算；n_trades/turnover/cost 为各标的合计（平均）。
    """
    per: List[BacktestMetrics] = []
    for sym in req.symbols:
        sub = BacktestRequest(
            strategy_id=req.strategy_id,
            params=req.params,
            symbols=[sym],
            start=req.start,
            end=req.end,
            timeframe=req.timeframe,
            capital=req.capital,
            extra=dict(req.extra),
        )
        per.append(_run_single(sub))

    eqs = [m.equity for m in per if m.equity is not None and m.equity.size > 0]
    if not eqs:
        return BacktestMetrics(0.0, 0.0, 1.0, 0)
    n = min(len(e) for e in eqs)
    comb = np.mean([e[:n] for e in eqs], axis=0)
    cm = metrics_from_equity(comb)
    cm.n_trades = int(sum(m.n_trades for m in per if m.n_trades > 0))
    cm.turnover = float(np.mean([m.turnover for m in per]))
    cm.cost_drag = float(sum(m.cost_drag for m in per))
    cm.equity = comb
    cm.raw = {
        "mode": "portfolio",
        "n_bars": min(m.raw.get("n_bars", 0) for m in per),
        "per_symbol": [
            {
                "symbol": sym,
                "sharpe": m.sharpe,
                "max_drawdown": m.max_drawdown,
                "total_return": m.total_return,
                "n_trades": m.n_trades,
                "turnover": m.turnover,
                "verdict": m.raw.get("verdict"),
            }
            for sym, m in zip(req.symbols, per)
        ],
    }
    return cm


def _run_single(req: BacktestRequest) -> BacktestMetrics:
    """单标的：引擎 validation（CPCV）+ 全窗口 simulate → 指标映射。"""
    bars = _resolve_bars(req)
    if len(bars) < _MIN_BARS:
        # 样本过短：引擎 CPCV 无意义，走 equity 兜底（n_trades=-1 → 门禁跳过交易数）
        equity = np.array([1.0] * max(len(bars) + 1, 3), dtype=float)
        m = metrics_from_equity(equity)
        m.raw["n_bars"] = len(bars)
        m.raw["verdict"] = "INCONCLUSIVE"
        m.raw["verdict_reason"] = f"only {len(bars)} bars < {_MIN_BARS}"
        return m

    strategy = _build_strategy(req)
    config = _engine_config(req)

    report = run_validation(strategy, bars, config=config)
    strategy.reset()  # 让全窗口 simulate 从冷启动开始，口径干净
    sim = simulate(strategy, bars, cost_model=config.cost_model)
    return _map_report_sim(report, sim, bars)
