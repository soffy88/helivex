"""gateway.deps — Shared dependencies: DB pool, path helpers, env."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import asyncpg

PROJECT_ROOT = Path(__file__).parent.parent

# Platform path injection (mirrors tools/strategy_gate.py)
for _p in (
    PROJECT_ROOT.parent.parent / "platform" / "3O" / "oprim",
    PROJECT_ROOT.parent.parent / "platform" / "3O" / "oskill",
    PROJECT_ROOT.parent.parent / "platform" / "3O" / "omodul",
):
    _ps = str(_p)
    if _ps not in sys.path:
        sys.path.insert(0, _ps)

DB_DSN      = os.environ.get("HELIVEX_DB_DSN", "postgresql://helios:helios_dev_pass@localhost:5434/helivex")
TRIAL_FILE  = PROJECT_ROOT / ".gate_trials.json"
STRATEGIES_DIR = PROJECT_ROOT / "strategies"

STRATEGY_YAML_MAP = {
    "trend_dual":   STRATEGIES_DIR / "trend_dual.yaml",
    "vwap_mr_dual": STRATEGIES_DIR / "vwap_mr_1h.yaml",
    "spot_trend":   STRATEGIES_DIR / "spot_trend_1d.yaml",
    "scalp_5m":     STRATEGIES_DIR / "scalp_5m.yaml",
}

# paper.signals strategy_id prefixes written by paper/strategies/*.py
STRATEGY_SIGNAL_PREFIX = {
    "trend_dual":   "donchian_4h_%",
    "vwap_mr_dual": "vwap_mr_1h_%",
    "spot_trend":   "spot_trend_1d_%",
    "scalp_5m":     "scalp_5m_%",
}

_pool: asyncpg.Pool | None = None


async def get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(DB_DSN, min_size=2, max_size=10)
    return _pool


async def close_pool() -> None:
    global _pool
    if _pool:
        await _pool.close()
        _pool = None


def load_trials() -> dict:
    if TRIAL_FILE.exists():
        with open(TRIAL_FILE) as f:
            return json.load(f)
    return {"total_trials": 0, "history": []}


def latest_verdict(strategy_id: str) -> str | None:
    data = load_trials()
    yaml_name = STRATEGY_YAML_MAP.get(strategy_id, Path(strategy_id)).name
    for entry in reversed(data.get("history", [])):
        cfg = entry.get("config", "")
        if strategy_id in cfg or yaml_name in cfg:
            return entry.get("verdict")
    return None
