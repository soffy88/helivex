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

DB_DSN = os.environ.get(
    "HELIVEX_DB_DSN", "postgresql://helios:helios_dev_pass@localhost:5434/helivex"
)
# 同一 platform-postgres 实例上的 iris/md 库(marketdata),LER 数据覆盖面板直接读,
# 不经 helivex 自己的 *_from_md.py adapter(那批 adapter 还没覆盖 liquidations/OI/1m)。
# gateway 跑在 helios-net 容器里(非 host 上的 systemd 进程),按 HELIVEX_DB_DSN 同款
# 网络内主机名(platform-postgres:5432,非 host 侧的 localhost:5434)。
MD_DSN = os.environ.get(
    "HELIVEX_MD_DSN",
    "postgresql://helios:helios_dev_pass@platform-postgres:5432/marketdata",
)
TRIAL_FILE = PROJECT_ROOT / ".gate_trials.json"
STRATEGIES_DIR = PROJECT_ROOT / "strategies"

STRATEGY_YAML_MAP = {
    "trend_dual": STRATEGIES_DIR / "trend_dual.yaml",
    "vwap_mr_dual": STRATEGIES_DIR / "vwap_mr_1h.yaml",
    "spot_trend": STRATEGIES_DIR / "spot_trend_1d.yaml",
    "scalp_5m": STRATEGIES_DIR / "scalp_5m.yaml",
    # 研究阶段占位(LER,见 strategies/ler_okx_swap.yaml 顶部注释)——无 paper/
    # strategies/*.py 实现,paper.signals 恒为 0 行,不是真在跑的策略。
    "ler_okx": STRATEGIES_DIR / "ler_okx_swap.yaml",
    # helixa trend_follower 移植(补齐 L)——observe:只记录信号不下单,过 gate 前 NO-GO
    "trend_follower_port": STRATEGIES_DIR / "trend_follower_port.yaml",
}

# paper.signals strategy_id prefixes written by paper/strategies/*.py
STRATEGY_SIGNAL_PREFIX = {
    "trend_dual": "donchian_4h_%",
    "vwap_mr_dual": "vwap_mr_1h_%",
    "spot_trend": "spot_trend_1d_%",
    "scalp_5m": "scalp_5m_%",
    "ler_okx": "ler_okx_%",  # 保留位——无实现,恒为 0 行
    "trend_follower_port": "trend_follower_port_%",  # 移植:observe 信号(无 fills)
}

_pool: asyncpg.Pool | None = None
_md_pool: asyncpg.Pool | None = None


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


async def get_md_pool() -> asyncpg.Pool:
    global _md_pool
    if _md_pool is None:
        _md_pool = await asyncpg.create_pool(MD_DSN, min_size=1, max_size=4)
    return _md_pool


async def close_md_pool() -> None:
    global _md_pool
    if _md_pool:
        await _md_pool.close()
        _md_pool = None


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
