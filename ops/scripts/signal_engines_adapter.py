"""Signal engines adapter — 跑 TA / ML / LLM 三引擎写 paper.engine_signals(3O 编排)。

helixa→helivex 3O 替换 Phase 4。计算在 platform/3O(oskill.signal + omodul.*);
本脚本读 OHLCV、跑引擎、落库。每引擎的 promoted 反映它是否过自己的门:
  - ta_multi:规则型多指标(EMA/MACD/RSI/BB/slope),多周期(5m+1h)对齐。透明、无过拟合
    风险 → promoted=True(仍受 P5 共识 + observe→enforce 分阶段治理)。
  - ml_lgb:LightGBM+三重障碍+walk-forward,DSR 门。promoted=<门结果>。真实数据上模型
    不 predictive(准确率≈0.5)→ 门正确拒绝晋级(observe-only)——这是超越 helixa 的点
    (它的门 VALIDATION_STRICT=false 从未生效,0.2315 的模型照投)。
  - llm_persona:LLM 人格槽,默认禁用(零成本)→ promoted=False。

用法: python ops/scripts/signal_engines_adapter.py [--once]
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
# md symbol(无 -SWAP)映射
_MD_SYMBOL = {i: i.replace("-SWAP", "") for i in INSTRUMENTS}
ML_ENABLED = {"BTC-USDT-SWAP", "ETH-USDT-SWAP", "SOL-USDT-SWAP"}

DDL = """
CREATE TABLE IF NOT EXISTS paper.engine_signals (
    id          BIGSERIAL PRIMARY KEY,
    cycle_ts    TIMESTAMPTZ NOT NULL,
    engine      TEXT NOT NULL,
    instrument  TEXT NOT NULL,
    direction   TEXT NOT NULL,
    score       NUMERIC,
    confidence  NUMERIC,
    promoted    BOOLEAN NOT NULL DEFAULT FALSE,
    detail      JSONB,
    fingerprint TEXT
);
CREATE INDEX IF NOT EXISTS engine_signals_cycle ON paper.engine_signals (cycle_ts DESC);
CREATE INDEX IF NOT EXISTS engine_signals_engine_inst ON paper.engine_signals (engine, instrument, cycle_ts DESC);
"""


async def _closes(md: asyncpg.Pool, symbol: str, tf: str) -> list[float]:
    rows = await md.fetch(
        """SELECT close FROM md.ohlcv
           WHERE venue='okx' AND instrument_type='perp' AND timeframe=$1 AND symbol=$2
           ORDER BY bar_open_ts ASC""",
        tf,
        symbol,
    )
    return [float(r["close"]) for r in rows]


async def _ohlcv(
    md: asyncpg.Pool, symbol: str, tf: str
) -> tuple[list[float], list[float], list[float]]:
    rows = await md.fetch(
        """SELECT high, low, close FROM md.ohlcv
           WHERE venue='okx' AND instrument_type='perp' AND timeframe=$1 AND symbol=$2
           ORDER BY bar_open_ts ASC""",
        tf,
        symbol,
    )
    return (
        [float(r["high"]) for r in rows],
        [float(r["low"]) for r in rows],
        [float(r["close"]) for r in rows],
    )


def _tf_trend(highs: list, lows: list, closes: list) -> dict | None:
    """helixa trend_follower(Donchian+ADX+Chandelier)。样本不足返回 None(诚实跳过)。"""
    from oskill.signal.trend_follower_signal import trend_follower_signal

    try:
        return trend_follower_signal(highs, lows, closes)
    except ValueError:
        return None


def _tf_scalp(closes: list, highs: list, lows: list) -> dict | None:
    """helixa intraday_scalper_v2(ADX 双模 breakout/mean-reversion)。"""
    from oskill.signal.intraday_scalper_signal import intraday_scalper_signal

    try:
        return intraday_scalper_signal(closes, highs, lows)
    except ValueError:
        return None


def _ta_signal(closes_by_tf: dict[str, list[float]]) -> dict:
    """多周期 TA 对齐:各 tf 跑 ta_multi_indicator_signal,分数取均值。"""
    from oskill.signal.ta_multi_indicator_signal import ta_multi_indicator_signal

    per_tf = {}
    scores = []
    for tf, closes in closes_by_tf.items():
        try:
            r = ta_multi_indicator_signal(closes)
            per_tf[tf] = {"score": r["score"], "votes": r["votes"]}
            scores.append(r["score"])
        except ValueError:
            continue
    if not scores:
        return {
            "direction": "neutral",
            "score": 0.0,
            "confidence": 0.0,
            "detail": {"per_tf": per_tf},
        }
    score = sum(scores) / len(scores)
    direction = "long" if score > 0 else ("short" if score < 0 else "neutral")
    return {
        "direction": direction,
        "score": score,
        "confidence": abs(score),
        "detail": {"per_tf": per_tf},
    }


async def run_once(hv: asyncpg.Pool, md: asyncpg.Pool) -> list[dict]:
    from omodul.ml_signal_workflow import MlSignalConfig, ml_signal_workflow
    from omodul.llm_signal_workflow import LlmSignalConfig, llm_signal_workflow

    async with hv.acquire() as conn:
        await conn.execute(DDL)

    cycle_ts = datetime.now(timezone.utc)
    out_dir = Path("/tmp/helivex_signal_engine_reports")
    written: list[dict] = []

    async with hv.acquire() as conn:
        for inst in INSTRUMENTS:
            sym = _MD_SYMBOL[inst]
            closes_5m = await _closes(md, sym, "m5")
            closes_1h = await _closes(md, sym, "h1")

            # TA
            ta = _ta_signal({"m5": closes_5m, "h1": closes_1h})
            await conn.execute(
                """INSERT INTO paper.engine_signals
                   (cycle_ts, engine, instrument, direction, score, confidence, promoted, detail, fingerprint)
                   VALUES ($1,'ta_multi',$2,$3,$4,$5,TRUE,$6,NULL)""",
                cycle_ts,
                inst,
                ta["direction"],
                ta["score"],
                ta["confidence"],
                json.dumps(ta["detail"], default=str),
            )
            written.append({"engine": "ta_multi", "inst": inst, **ta})

            # trend_follower(P7,Donchian+ADX+Chandelier,h4)+ intraday_scalper(5m 双模)
            h4, l4, c4 = await _ohlcv(md, sym, "h4")
            h5, l5, c5 = await _ohlcv(md, sym, "m5")
            for engine_name, fn in (
                ("tf_trend", lambda: _tf_trend(h4, l4, c4)),
                ("tf_scalp", lambda: _tf_scalp(c5, h5, l5)),
            ):
                sig = fn()
                if sig is None:
                    continue
                await conn.execute(
                    """INSERT INTO paper.engine_signals
                       (cycle_ts, engine, instrument, direction, score, confidence, promoted, detail, fingerprint)
                       VALUES ($1,$2,$3,$4,$5,$6,TRUE,$7,NULL)""",
                    cycle_ts,
                    engine_name,
                    inst,
                    sig["direction"],
                    sig["score"],
                    sig["confidence"],
                    json.dumps(sig.get("votes", {}), default=str),
                )
                written.append({"engine": engine_name, "inst": inst, **sig})

            # ML(gated)
            if inst in ML_ENABLED and len(closes_5m) > 600:
                r = ml_signal_workflow(
                    MlSignalConfig(symbol=inst), {"closes": closes_5m}, out_dir
                )
                if r["status"] == "completed":
                    f = r["findings"]
                    await conn.execute(
                        """INSERT INTO paper.engine_signals
                           (cycle_ts, engine, instrument, direction, score, confidence, promoted, detail, fingerprint)
                           VALUES ($1,'ml_lgb',$2,$3,$4,$5,$6,$7,$8)""",
                        cycle_ts,
                        inst,
                        f["direction"],
                        f["score"],
                        f["confidence"],
                        f["promoted"],
                        json.dumps(
                            {
                                k: f[k]
                                for k in (
                                    "wfv_accuracy",
                                    "oos_sharpe",
                                    "dsr",
                                    "deflated_sharpe",
                                    "n_folds",
                                    "promoted",
                                )
                            },
                            default=str,
                        ),
                        r["fingerprint"],
                    )
                    written.append(
                        {
                            "engine": "ml_lgb",
                            "inst": inst,
                            "direction": f["direction"],
                            "promoted": f["promoted"],
                            "dsr": f["dsr"],
                        }
                    )

            # LLM(disabled slot)
            lr = llm_signal_workflow(LlmSignalConfig(symbol=inst), {}, out_dir)
            if lr["status"] == "completed":
                f = lr["findings"]
                await conn.execute(
                    """INSERT INTO paper.engine_signals
                       (cycle_ts, engine, instrument, direction, score, confidence, promoted, detail, fingerprint)
                       VALUES ($1,'llm_persona',$2,$3,$4,$5,$6,$7,$8)""",
                    cycle_ts,
                    inst,
                    f["direction"],
                    f["score"],
                    f["confidence"],
                    f["promoted"],
                    json.dumps(
                        {"enabled": f["enabled"], "note": f["note"]}, default=str
                    ),
                    lr["fingerprint"],
                )
    return written


async def main(once: bool) -> None:
    hv = await asyncpg.create_pool(HV_DSN, min_size=1, max_size=2)
    md = await asyncpg.create_pool(MD_DSN, min_size=1, max_size=2)
    try:
        written = await run_once(hv, md)
        for w in written:
            extra = (
                f" promoted={w['promoted']} dsr={w.get('dsr'):.4f}"
                if "promoted" in w and w["engine"] == "ml_lgb"
                else ""
            )
            print(
                f"signal_engines: {w['engine']:12s} {w['inst']:16s} {w['direction']}{extra}"
            )
    finally:
        await hv.close()
        await md.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    asyncio.run(main(args.once))
