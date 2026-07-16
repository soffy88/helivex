"""Consensus→risk adapter — 把共识过全套风控管线(observe),并跑归因→权重学习。

helixa→helivex 3O 替换 Phase 6。两件事:
  1. 对每条最新共识信号,过 oskill.risk.consensus_risk_size(crisis override + P1 三层
     裁剪 + fee/edge),算出"这笔会不会过风控、多大仓位",写 paper.consensus_risk_eval。
     **observe-only**:只记录、不下单——是共识(P5)到实盘执行之间的观察桥。真正接
     enforce(下 paper 单)是后续独立、需人工放行的一步(同 P1 Stage B)。
  2. 归因→权重学习闭环:读引擎驱动的 round-trip(带引擎标签)→ oskill.consensus.
     engine_attribution → ewma_weight_update → 更新 paper.engine_weights。当前引擎还没
     驱动实盘(共识仍 observe),故无带标签成交,权重维持 base——机制就位,有数据即生效。

用法: python ops/scripts/consensus_risk_adapter.py [--once]
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

from paper.risk import nav_and_drawdown, open_positions  # noqa: E402

HV_DSN = os.environ.get(
    "DB_DSN", "postgresql://helios:helios_dev_pass@localhost:5434/helivex"
)
MD_DSN = os.environ.get(
    "MD_DSN", "postgresql://helios:helios_dev_pass@localhost:5434/marketdata"
)
CORRELATION_PAIRS = {
    "BTC-USDT-SWAP": {"ETH-USDT-SWAP": 0.85},
    "ETH-USDT-SWAP": {"BTC-USDT-SWAP": 0.85},
}
BASE_WEIGHTS = {
    "ta_multi": 1.0,
    "tf_trend": 1.0,
    "tf_scalp": 1.0,
    "ml_lgb": 1.8,
    "llm_persona": 1.2,
}
ATR_PERIOD = 14

DDL = """
CREATE TABLE IF NOT EXISTS paper.consensus_risk_eval (
    id               BIGSERIAL PRIMARY KEY,
    cycle_ts         TIMESTAMPTZ NOT NULL,
    instrument       TEXT NOT NULL,
    direction        TEXT NOT NULL,
    should_execute   BOOLEAN,
    approved         BOOLEAN,
    final_notional   NUMERIC,
    blocking_stage   TEXT,
    crisis_scaled    BOOLEAN,
    enforce_mode     TEXT NOT NULL,
    reasons          JSONB,
    detail           JSONB
);
CREATE INDEX IF NOT EXISTS consensus_risk_eval_cycle ON paper.consensus_risk_eval (cycle_ts DESC);
"""

ENFORCE_MODE = os.environ.get(
    "HELIVEX_CONSENSUS_ENFORCE", "observe"
)  # observe | enforce


async def _atr_pct(md: asyncpg.Pool, symbol: str) -> float:
    from oprim import atr

    async with md.acquire() as conn:
        rows = await conn.fetch(
            """SELECT high, low, close FROM md.ohlcv
               WHERE venue='okx' AND instrument_type='perp' AND timeframe='m5' AND symbol=$1
               ORDER BY bar_open_ts DESC LIMIT $2""",
            symbol,
            ATR_PERIOD + 30,
        )
    if len(rows) < ATR_PERIOD + 1:
        return 0.0
    rows = list(reversed(rows))
    highs = [float(r["high"]) for r in rows]
    lows = [float(r["low"]) for r in rows]
    closes = [float(r["close"]) for r in rows]
    try:
        a = atr(highs, lows, closes, period=ATR_PERIOD)
    except ValueError:
        return 0.0
    return a / closes[-1] if closes[-1] > 0 else 0.0


async def _positions_usd(hv: asyncpg.Pool) -> dict[str, float]:
    async with hv.acquire() as conn:
        pos = await open_positions(conn)
    out: dict[str, float] = {}
    for (_s, inst), (qty, cost) in pos.items():
        out[inst] = out.get(inst, 0.0) + abs(qty * cost)
    return out


async def _optimal_weights(hv: asyncpg.Pool) -> dict[str, float]:
    async with hv.acquire() as conn:
        rows = await conn.fetch(
            """SELECT instrument, weight FROM paper.portfolio_weights
               WHERE cycle_ts=(SELECT MAX(cycle_ts) FROM paper.portfolio_weights)"""
        )
    return {r["instrument"]: float(r["weight"]) for r in rows}


async def _update_weights(hv: asyncpg.Pool) -> None:
    """归因→权重学习闭环。当前无引擎标签成交(共识 observe)→ round_trips 为空,
    权重维持 base。机制就位:一旦有带 engines 标签的 round-trip,即按 EWMA 更新。"""
    from oskill.consensus.engine_attribution import engine_attribution
    from oskill.consensus.ewma_weight_update import ewma_weight_update

    round_trips: list[dict] = []  # 引擎标签成交出现前为空(见 docstring)
    attrib = engine_attribution(round_trips)
    async with hv.acquire() as conn:
        for eng, seed in BASE_WEIGHTS.items():
            a = attrib.get(eng)
            row = await conn.fetchrow(
                "SELECT base_weight, accuracy FROM paper.engine_weights WHERE engine=$1",
                eng,
            )
            # DB base_weight 是权威源(可经 gateway PUT /engines/weights 在线调),
            # 不存在才用硬编码 seed。EWMA 从这个 base 起算,故手动改的权重不被覆盖。
            base = (
                float(row["base_weight"])
                if row and row["base_weight"] is not None
                else seed
            )
            prior = (
                float(row["accuracy"]) if row and row["accuracy"] is not None else 0.5
            )
            upd = ewma_weight_update(prior, a["results"] if a else [], base_weight=base)
            await conn.execute(
                """INSERT INTO paper.engine_weights (engine, base_weight, accuracy, dyn_weight, updated_at)
                   VALUES ($1,$2,$3,$4,now())
                   ON CONFLICT (engine) DO UPDATE SET accuracy=$3, dyn_weight=$4, updated_at=now()""",
                eng,
                base,
                upd["accuracy"],
                upd["dynamic_weight"],
            )


async def run_once(hv: asyncpg.Pool, md: asyncpg.Pool) -> list[dict]:
    from oskill.risk.consensus_risk_size import consensus_risk_size

    async with hv.acquire() as conn:
        await conn.execute(DDL)
        cons_rows = await conn.fetch(
            """SELECT instrument, final_direction, consensus_score, kelly_position,
                      should_execute, regime_state
               FROM paper.consensus_signals
               WHERE cycle_ts=(SELECT MAX(cycle_ts) FROM paper.consensus_signals)"""
        )

    positions = await _positions_usd(hv)
    opt_w = await _optimal_weights(hv)
    async with hv.acquire() as conn:
        nav = await nav_and_drawdown(conn)
    capital = nav["nav"]

    cycle_ts = datetime.now(timezone.utc)
    results = []
    async with hv.acquire() as conn:
        for r in cons_rows:
            inst = r["instrument"]
            sym = inst.replace("-SWAP", "")
            atr_pct = await _atr_pct(md, sym)
            corr = [
                (positions.get(o, 0.0) / capital if capital else 0.0, c)
                for o, c in CORRELATION_PAIRS.get(inst, {}).items()
            ]
            ev = consensus_risk_size(
                direction=r["final_direction"],
                kelly_position=float(r["kelly_position"] or 0.0),
                should_execute=r["should_execute"],
                capital_usd=capital,
                current_position_usd=positions.get(inst, 0.0),
                atr_pct=atr_pct,
                regime_state=r["regime_state"] or "range",
                optimal_weight=opt_w.get(inst, 1.0 / max(1, len(cons_rows))),
                correlated_positions=corr,
            )
            await conn.execute(
                """INSERT INTO paper.consensus_risk_eval
                   (cycle_ts, instrument, direction, should_execute, approved, final_notional,
                    blocking_stage, crisis_scaled, enforce_mode, reasons, detail)
                   VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)""",
                cycle_ts,
                inst,
                r["final_direction"],
                r["should_execute"],
                ev["approved"],
                ev["final_notional"],
                ev["blocking_stage"],
                ev["crisis_scaled"],
                ENFORCE_MODE,
                json.dumps(ev["reasons"], default=str),
                json.dumps(
                    {"tiers": ev["tiers"], "fee_edge": ev["fee_edge"]}, default=str
                ),
            )
            results.append({"inst": inst, "should_execute": r["should_execute"], **ev})

    await _update_weights(hv)
    return results


async def main(once: bool) -> None:
    hv = await asyncpg.create_pool(HV_DSN, min_size=1, max_size=2)
    md = await asyncpg.create_pool(MD_DSN, min_size=1, max_size=2)
    try:
        print(f"enforce_mode={ENFORCE_MODE}")
        for r in await run_once(hv, md):
            print(
                f"consensus_risk: {r['inst']:16s} exec={r['should_execute']} approved={r['approved']} "
                f"notional={r['final_notional']:.2f} stage={r['blocking_stage']} crisis={r['crisis_scaled']}"
            )
    finally:
        await hv.close()
        await md.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    asyncio.run(main(args.once))
