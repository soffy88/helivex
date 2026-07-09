"""Consensus adapter — 多引擎共识"大脑"(3O 编排)。

helixa→helivex 3O 替换 Phase 5。融合:
  - 引擎信号(paper.engine_signals,P4:ta_multi/ml_lgb/llm_persona)
  - regime(paper.regime_state,P2,advisory)
  - 情绪 FGI + 链上(marketdata.md.sentiment/md.onchain,P3)
  - 每引擎权重(paper.engine_weights,EWMA 学习;无归因数据时用 base)
→ omodul.consensus_workflow → paper.consensus_signals。

**门纪律**:只有 promoted=True 的引擎驱动可执行共识(should_execute);未晋级引擎
记录但不投票——与 helixa(什么都投)的关键区别。共识本身也 observe-only,接实盘由
P6 风控 + observe→enforce 分阶段治理。

用法: python ops/scripts/consensus_adapter.py [--once]
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
MD_DSN = os.environ.get(
    "MD_DSN", "postgresql://helios:helios_dev_pass@localhost:5434/marketdata"
)
INSTRUMENTS = ["BTC-USDT-SWAP", "ETH-USDT-SWAP", "SOL-USDT-SWAP"]
_ONCHAIN_ASSET = {"BTC-USDT-SWAP": "btc", "ETH-USDT-SWAP": "eth"}  # SOL 无链上
# base weights(helixa 口径:qlib_v2=1.8, tv=1.0, llm=1.2)映射到 helivex 引擎
BASE_WEIGHTS = {"ta_multi": 1.0, "tf_trend": 1.0, "tf_scalp": 1.0, "ml_lgb": 1.8, "llm_persona": 1.2}

DDL = """
CREATE TABLE IF NOT EXISTS paper.consensus_signals (
    id                BIGSERIAL PRIMARY KEY,
    cycle_ts          TIMESTAMPTZ NOT NULL,
    instrument        TEXT NOT NULL,
    final_direction   TEXT NOT NULL,
    consensus_score   NUMERIC,
    kelly_position    NUMERIC,
    agreement_ratio   NUMERIC,
    is_divergent      BOOLEAN,
    should_execute    BOOLEAN,
    n_promoted        INT,
    regime_state      TEXT,
    sentiment_bias    NUMERIC,
    onchain_bias      NUMERIC,
    fingerprint       TEXT,
    detail            JSONB
);
CREATE INDEX IF NOT EXISTS consensus_signals_cycle ON paper.consensus_signals (cycle_ts DESC);

CREATE TABLE IF NOT EXISTS paper.engine_weights (
    engine      TEXT PRIMARY KEY,
    base_weight NUMERIC NOT NULL,
    accuracy    NUMERIC,
    dyn_weight  NUMERIC NOT NULL,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""


async def _latest_engine_signals(hv: asyncpg.Pool, inst: str) -> list[dict]:
    async with hv.acquire() as conn:
        rows = await conn.fetch(
            """SELECT engine, direction, score, confidence, promoted, cycle_ts
               FROM paper.engine_signals
               WHERE instrument=$1 AND cycle_ts=(SELECT MAX(cycle_ts) FROM paper.engine_signals WHERE instrument=$1)""",
            inst,
        )
    now = datetime.now(timezone.utc)
    return [
        {
            "engine": r["engine"],
            "direction": r["direction"],
            "score": float(r["score"]) if r["score"] is not None else 0.0,
            "confidence": float(r["confidence"])
            if r["confidence"] is not None
            else 0.0,
            "promoted": r["promoted"],
            "age_seconds": (now - r["cycle_ts"]).total_seconds(),
        }
        for r in rows
    ]


async def _regime(hv: asyncpg.Pool, inst: str) -> str:
    async with hv.acquire() as conn:
        st = await conn.fetchval(
            """SELECT state FROM paper.regime_state
               WHERE instrument=$1 ORDER BY cycle_ts DESC LIMIT 1""",
            inst,
        )
    return st or "range"


async def _weights(hv: asyncpg.Pool) -> dict[str, float]:
    """Dynamic weights from paper.engine_weights; seed base rows if absent."""
    async with hv.acquire() as conn:
        for eng, base in BASE_WEIGHTS.items():
            await conn.execute(
                """INSERT INTO paper.engine_weights (engine, base_weight, accuracy, dyn_weight)
                   VALUES ($1,$2,0.5,$2) ON CONFLICT (engine) DO NOTHING""",
                eng,
                base,
            )
        rows = await conn.fetch("SELECT engine, dyn_weight FROM paper.engine_weights")
    return {r["engine"]: float(r["dyn_weight"]) for r in rows}


async def _sentiment_onchain(
    md: asyncpg.Pool, inst: str
) -> tuple[float | None, dict | None]:
    async with md.acquire() as conn:
        fgi = await conn.fetchval(
            """SELECT value FROM md.sentiment
               WHERE source='alternative.me' AND metric='fear_greed_index'
               ORDER BY ts DESC LIMIT 1"""
        )
        asset = _ONCHAIN_ASSET.get(inst)
        onchain = None
        if asset:
            rows = await conn.fetch(
                """SELECT metric, value FROM md.onchain
                   WHERE asset=$1 AND ts=(SELECT MAX(ts) FROM md.onchain WHERE asset=$1)""",
                asset,
            )
            m = {r["metric"]: float(r["value"]) for r in rows if r["value"] is not None}
            if m:
                onchain = {
                    "flow_in": m.get("FlowInExNtv", 0.0),
                    "flow_out": m.get("FlowOutExNtv", 0.0),
                    "mvrv": m.get("CapMVRVCur", 1.0),
                }
    return (float(fgi) if fgi is not None else None), onchain


async def run_once(hv: asyncpg.Pool, md: asyncpg.Pool) -> list[dict]:
    from omodul.consensus_workflow import ConsensusConfig, consensus_workflow

    async with hv.acquire() as conn:
        await conn.execute(DDL)

    weights = await _weights(hv)
    cycle_ts = datetime.now(timezone.utc)
    out_dir = Path("/tmp/helivex_consensus_reports")
    results = []

    async with hv.acquire() as conn:
        for inst in INSTRUMENTS:
            signals = await _latest_engine_signals(hv, inst)
            if not signals:
                continue
            regime_state = await _regime(hv, inst)
            fgi, onchain = await _sentiment_onchain(md, inst)
            r = consensus_workflow(
                ConsensusConfig(instrument=inst),
                {
                    "signals": signals,
                    "weights": weights,
                    "regime_state": regime_state,
                    "fgi": fgi,
                    "onchain": onchain,
                },
                out_dir,
            )
            if r["status"] != "completed":
                continue
            f = r["findings"]
            await conn.execute(
                """INSERT INTO paper.consensus_signals
                   (cycle_ts, instrument, final_direction, consensus_score, kelly_position,
                    agreement_ratio, is_divergent, should_execute, n_promoted, regime_state,
                    sentiment_bias, onchain_bias, fingerprint, detail)
                   VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14)""",
                cycle_ts,
                inst,
                f["final_direction"],
                f["consensus_score"],
                f["kelly_position"],
                f["agreement_ratio"],
                f["is_divergent"],
                f["should_execute"],
                f["n_promoted"],
                regime_state,
                f["sentiment_bias"],
                f["onchain_bias"],
                r["fingerprint"],
                json.dumps(
                    {
                        "contributions": f["contributions"],
                        "live_score": f["live_score"],
                        "effective_threshold": f["effective_threshold"],
                    },
                    default=str,
                ),
            )
            results.append({"inst": inst, "regime_state": regime_state, **f})
    return results


async def main(once: bool) -> None:
    hv = await asyncpg.create_pool(HV_DSN, min_size=1, max_size=2)
    md = await asyncpg.create_pool(MD_DSN, min_size=1, max_size=2)
    try:
        for r in await run_once(hv, md):
            print(
                f"consensus: {r['inst']:16s} {r['final_direction']:8s} score={r['consensus_score']:+.3f} "
                f"kelly={r['kelly_position']:.3f} execute={r['should_execute']} promoted={r['n_promoted']} regime={r.get('regime_state', '?')}"
            )
    finally:
        await hv.close()
        await md.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    asyncio.run(main(args.once))
