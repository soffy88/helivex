"""审计：每次 optimize 追加一行 JSONL（CC 规格 §7）。

记录 best_params / utility / accepted / gate_reason / 指标（train + test）+ 调用方 meta。
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Dict


def _metrics_blob(m) -> Dict[str, Any]:
    if m is None:
        return None
    return {
        "sharpe": m.sharpe,
        "mdd": m.max_drawdown,
        "ret": m.total_return,
        "n_trades": m.n_trades,
        "turnover": m.turnover,
        "cost_drag": m.cost_drag,
    }


def log_optimize(path: str, result, meta: Dict[str, Any]) -> None:
    """追加审计记录（线程不安全；一次 optimize 一条，append 模式）。"""
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    rec = {
        "ts": time.time(),
        "best_params": result.best_params,
        "utility": result.best_utility,
        "accepted": result.accepted,
        "gate_reason": result.gate_reason,
        "metrics": _metrics_blob(result.best_metrics),
        "test_metrics": _metrics_blob(getattr(result, "test_metrics", None)),
        "n_evals": len(result.history),
        "meta": meta,
    }
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
