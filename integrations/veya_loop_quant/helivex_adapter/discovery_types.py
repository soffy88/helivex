"""协议层：与 Helivex 解耦的稳定接口（CC 规格 §3.1）。

本模块不 import 任何 Helivex / Veya 内部包，仅定义跨层数据契约。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

import numpy as np


@dataclass
class BacktestRequest:
    strategy_id: str
    params: Dict[str, float]          # 连续/可数值化参数（SignalConfig 字段名）
    symbols: Sequence[str]
    start: str                        # ISO date（含），按 bar_close_ts >= start 过滤
    end: str                          # ISO date（不含），按 bar_close_ts <  end 过滤
    timeframe: str = "1d"
    capital: float = 1_000_000.0
    extra: Dict[str, Any] = field(default_factory=dict)
    # extra 约定（backtest_bridge 消费）：
    #   bars:         list[BarRow]（跳过 parquet 读取，测试/离线用）
    #   parquet_path: str/Path（数据文件；缺省时在 data_dir 按 symbol+timeframe 自动查找）
    #   data_dir:     str/Path（自动查找的根目录，默认 helivex/data）
    #   config_grid:  dict[str, list]  — 声明给引擎的搜索广度（DSR 通缩用 n_trials 口径）
    #   cost_model:   dict — CostModel(taker_fee_bps, maker_fee_bps, ...) 覆写
    #   cpcv:         dict — CPCVConfig(n_groups, n_test_groups, embargo_bars, ...) 覆写


@dataclass
class BacktestMetrics:
    total_return: float
    sharpe: float
    max_drawdown: float               # 统一约定：正数表示回撤幅度，0.15 = 15%
    n_trades: int
    win_rate: float = float("nan")
    turnover: float = float("nan")    # 年化 one-way 换手（倍/年）
    cost_drag: float = float("nan")   # 相对初始资本的总交易成本
    equity: Optional[np.ndarray] = None
    raw: Dict[str, Any] = field(default_factory=dict)


@dataclass
class OptimizeResult:
    best_params: Dict[str, float]
    best_metrics: BacktestMetrics
    best_utility: float
    history: List[Dict[str, Any]]
    accepted: bool
    gate_reason: str
    test_metrics: Optional[BacktestMetrics] = None   # test_window 门禁时的测试集指标
