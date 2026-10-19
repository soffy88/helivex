"""helivex_adapter — Veya Loop × Helivex 量化交易适配层。

职责边界（HELIVEX_QUANT_VEYA_LOOP_CC_SPEC.md）：
- Helivex（helios_backtest_v51 引擎 + run_features 数据管线）负责数据与回测真相；
- Veya Loop 负责 `bayesian_optimize` 参数搜索、效用评价、风险门禁与 JSONL 审计；
- 本包不重写回测引擎、不直连实盘（研究态）。

唯一允许 import Helivex 内部 API 的文件是 `backtest_bridge.py`。
"""

from helivex_adapter.discovery_types import (
    BacktestMetrics,
    BacktestRequest,
    OptimizeResult,
)
from helivex_adapter.risk_gate import RiskLimits, risk_gate, utility
from helivex_adapter.metrics import metrics_from_equity

__all__ = [
    "BacktestMetrics",
    "BacktestRequest",
    "OptimizeResult",
    "RiskLimits",
    "metrics_from_equity",
    "risk_gate",
    "utility",
]
