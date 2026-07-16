"""Regime adapter — 每 instrument 的市场状态识别(crisis/trend/range),3O 编排。

Phase 2 of the helixa→helivex 3O replacement (docs/HELIXA_REPLACEMENT_PLAN.md)。
计算在 platform/3O 的 oskill.regime + omodul.regime_workflow;本脚本只读 OHLCV、
调 omodul、写 paper.regime_state。

**门禁纪律**:helivex 自己的研究(commit 646dc71,11/11 FAIL)证 HMM 市场 regime
无 OOS 持续性 → regime 输出为**咨询性 soft input**,advisory=true,不作硬门;
消费方(共识/风控)把它当众多输入之一,不能仅凭 regime 拦/驱动实盘。默认用确定性
分类器(不依赖 hmmlearn);method=hmm 时缺 hmmlearn 自动回退。

用法: python ops/scripts/regime_adapter.py [--once]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import asyncpg

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

HV_DSN = os.environ.get(
    "DB_DSN", "postgresql://helios:helios_dev_pass@localhost:5434/helivex"
)
REGIME_METHOD = os.environ.get("HELIVEX_REGIME_METHOD", "deterministic")
INSTRUMENTS = ["BTC-USDT-SWAP", "ETH-USDT-SWAP", "SOL-USDT-SWAP"]

DDL = """
CREATE TABLE IF NOT EXISTS paper.regime_state (
    id              BIGSERIAL PRIMARY KEY,
    cycle_ts        TIMESTAMPTZ NOT NULL,
    instrument      TEXT NOT NULL,
    state           TEXT NOT NULL,
    confidence      NUMERIC,
    method_used     TEXT,
    rows_used       INT,
    advisory        BOOLEAN NOT NULL DEFAULT TRUE,
    fingerprint     TEXT,
    detail          JSONB,
    decision_trail  JSONB
);
CREATE INDEX IF NOT EXISTS regime_state_cycle_ts
    ON paper.regime_state (cycle_ts DESC);
"""


async def load_closes(conn: asyncpg.Connection, instrument: str) -> list[float]:
    """5m okx_swap closes for one instrument, chronological (most bars available)."""
    rows = await conn.fetch(
        """SELECT close FROM market_data.ohlcv_5m
           WHERE source = 'okx_swap_5m' AND instrument = $1
           ORDER BY bar_close_ts ASC""",
        instrument,
    )
    return [float(r["close"]) for r in rows]


async def run_once(hv: asyncpg.Pool) -> dict:
    from omodul.regime_workflow import RegimeConfig, regime_workflow

    async with hv.acquire() as conn:
        await conn.execute(DDL)
        closes_by_inst = {inst: await load_closes(conn, inst) for inst in INSTRUMENTS}

    cycle_ts = datetime.now(timezone.utc)
    output_dir = Path("/tmp/helivex_regime_reports")
    results: dict[str, dict] = {}

    async with hv.acquire() as conn:
        for inst, closes in closes_by_inst.items():
            cfg = RegimeConfig(method=REGIME_METHOD, symbol=inst)
            r = regime_workflow(cfg, {"closes": closes}, output_dir)
            results[inst] = r
            if r["status"] != "completed":
                continue
            f = r["findings"]
            await conn.execute(
                """INSERT INTO paper.regime_state
                   (cycle_ts, instrument, state, confidence, method_used, rows_used,
                    advisory, fingerprint, detail, decision_trail)
                   VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)""",
                cycle_ts,
                inst,
                f["state"],
                f["confidence"],
                f["method_used"],
                f["rows_used"],
                f["advisory"],
                r["fingerprint"],
                json.dumps(f["detail"], default=str),
                json.dumps(r["decision_trail"], default=str),
            )

    return results


async def main(once: bool) -> None:
    hv = await asyncpg.create_pool(HV_DSN, min_size=1, max_size=2)
    try:
        results = await run_once(hv)
        for inst, r in results.items():
            if r["status"] == "completed":
                f = r["findings"]
                print(
                    f"regime_adapter: {inst} -> {f['state']} "
                    f"(conf {f['confidence']:.3f}, {f['method_used']}, {f['rows_used']} bars)"
                )
            else:
                print(f"regime_adapter: {inst} FAILED: {r['error']}")
    finally:
        await hv.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    asyncio.run(main(args.once))
