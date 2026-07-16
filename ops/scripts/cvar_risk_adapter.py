"""CVaR 组合优化 + 动态风控 adapter(3O 编排,helivex 侧只管 DB I/O + 调度)。

Phase 1 of "3O 重建 helixa 能力"(见会话内 plan `curried-mixing-crab.md`)。计算逻辑
全部在 platform/3O 的 oprim/oskill/omodul 里(纯函数 + 四支柱 omodul),本脚本只做:
读 market_data.ohlcv_1h(OKX swap,30 天小时收益率 + ATR)、读 paper.risk 的当前持仓/NAV
(复用 paper.risk.open_positions/nav_and_drawdown,不新造第 4 套 P&L 计算)、调用
omodul.portfolio.cvar_risk_workflow、写 paper.portfolio_weights + paper.position_caps。

**Stage A(当前阶段,观察期)**:只计算、只落库、前端可见,完全不影响
paper.risk.gate_entry 的真实放行结果 —— paper/strategies/*.py 零改动。

用法:
    python ops/scripts/cvar_risk_adapter.py [--once]
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import asyncpg
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from paper.risk import RISK, nav_and_drawdown, open_positions  # noqa: E402

HV_DSN = os.environ.get(
    "DB_DSN", "postgresql://helios:helios_dev_pass@localhost:5434/helivex"
)

INSTRUMENTS = ["BTC-USDT-SWAP", "ETH-USDT-SWAP", "SOL-USDT-SWAP"]
LOOKBACK_DAYS = 30
MIN_OBS = 50
ATR_PERIOD = 14
CVAR_ALPHA = 0.05

# spec §11-style locked defaults for this phase — Stage B enforcement values,
# irrelevant to Stage A (observe-only), kept here so the workflow's output is
# consistent once Stage B is turned on.
ATR_RISK_BUDGET = 0.01
ATR_MIN_POSITION = 0.005
ATR_MAX_POSITION = 0.20
CORRELATION_PAIRS = {
    "BTC-USDT-SWAP": {"ETH-USDT-SWAP": 0.85},
    "ETH-USDT-SWAP": {"BTC-USDT-SWAP": 0.85},
}
MAX_NET_EXPOSURE = 0.25
MIN_TRADE_NOTIONAL = 10.0

DDL = """
CREATE TABLE IF NOT EXISTS paper.portfolio_weights (
    id                 BIGSERIAL PRIMARY KEY,
    cycle_ts           TIMESTAMPTZ NOT NULL,
    instrument         TEXT NOT NULL,
    weight             NUMERIC NOT NULL,
    method             TEXT NOT NULL,
    fallback_reason    TEXT,
    portfolio_cvar_95  NUMERIC,
    lookback_days      INT,
    n_obs              INT,
    fingerprint        TEXT,
    decision_trail     JSONB
);
CREATE INDEX IF NOT EXISTS portfolio_weights_cycle_ts
    ON paper.portfolio_weights (cycle_ts DESC);

CREATE TABLE IF NOT EXISTS paper.position_caps (
    id                 BIGSERIAL PRIMARY KEY,
    cycle_ts           TIMESTAMPTZ NOT NULL,
    instrument         TEXT NOT NULL,
    tier1_headroom     NUMERIC,
    tier2_atr_cap      NUMERIC,
    tier3_corr_clip    NUMERIC,
    effective_cap_usd  NUMERIC,
    binding_tier       TEXT,
    reasons            JSONB
);
CREATE INDEX IF NOT EXISTS position_caps_cycle_ts
    ON paper.position_caps (cycle_ts DESC);
"""


async def load_returns(conn: asyncpg.Connection) -> pd.DataFrame:
    """30-day hourly close-price pct_change returns, OKX swap, wide (instrument columns)."""
    rows = await conn.fetch(
        """
        SELECT instrument, bar_close_ts, close
        FROM market_data.ohlcv_1h
        WHERE source = 'okx_swap' AND instrument = ANY($1)
          AND bar_close_ts >= now() - ($2 || ' days')::interval
        ORDER BY bar_close_ts ASC
        """,
        INSTRUMENTS,
        str(LOOKBACK_DAYS),
    )
    if not rows:
        return pd.DataFrame(columns=INSTRUMENTS)
    df = pd.DataFrame(rows, columns=["instrument", "bar_close_ts", "close"])
    wide = df.pivot(index="bar_close_ts", columns="instrument", values="close").astype(
        float
    )
    return wide.pct_change().dropna()


async def compute_atr_pct(conn: asyncpg.Connection) -> dict[str, float]:
    """Current ATR/close per instrument, from the same OHLCV table (oprim.atr)."""
    from oprim import atr

    out: dict[str, float] = {}
    for inst in INSTRUMENTS:
        rows = await conn.fetch(
            """
            SELECT high, low, close FROM market_data.ohlcv_1h
            WHERE source = 'okx_swap' AND instrument = $1
            ORDER BY bar_close_ts DESC LIMIT $2
            """,
            inst,
            ATR_PERIOD + 30,
        )
        if len(rows) < ATR_PERIOD + 1:
            continue
        rows = list(reversed(rows))
        highs = [float(r["high"]) for r in rows]
        lows = [float(r["low"]) for r in rows]
        closes = [float(r["close"]) for r in rows]
        try:
            atr_val = atr(highs, lows, closes, period=ATR_PERIOD)
        except ValueError:
            continue
        if closes[-1] > 0:
            out[inst] = atr_val / closes[-1]
    return out


async def current_positions_usd(conn: asyncpg.Connection) -> dict[str, float]:
    """Aggregate open notional per instrument across all strategies (paper.risk)."""
    positions = await open_positions(conn)
    out: dict[str, float] = {inst: 0.0 for inst in INSTRUMENTS}
    for (_strategy_id, instrument), (qty, avg_cost) in positions.items():
        if instrument in out:
            out[instrument] += abs(qty * avg_cost)
    return out


async def run_once(hv: asyncpg.Pool) -> dict:
    async with hv.acquire() as conn:
        await conn.execute(DDL)
        returns = await load_returns(conn)
        atr_pct = await compute_atr_pct(conn)
        positions_usd = await current_positions_usd(conn)
        nav = await nav_and_drawdown(conn)

    capital_usd = nav["nav"]

    from omodul.portfolio import CvarRiskConfig, cvar_risk_workflow

    config = CvarRiskConfig(
        symbols=INSTRUMENTS,
        lookback_days=LOOKBACK_DAYS,
        alpha=CVAR_ALPHA,
        min_obs=MIN_OBS,
        atr_risk_budget=ATR_RISK_BUDGET,
        atr_min_position=ATR_MIN_POSITION,
        atr_max_position=ATR_MAX_POSITION,
        correlation_pairs=CORRELATION_PAIRS,
        max_net_exposure=MAX_NET_EXPOSURE,
        min_trade_notional=MIN_TRADE_NOTIONAL,
    )
    input_data = {
        "returns": returns,
        "current_positions_usd": positions_usd,
        "capital_usd": capital_usd,
        "atr_pct": atr_pct,
    }
    output_dir = Path("/tmp/helivex_cvar_risk_reports")
    result = cvar_risk_workflow(config, input_data, output_dir)

    import json

    cycle_ts = datetime.now(timezone.utc)
    async with hv.acquire() as conn:
        if result["status"] == "completed":
            weights_result = result["findings"]["weights"]
            caps = result["findings"]["position_caps"]
            fingerprint = result["fingerprint"]
            trail_json = json.dumps(result["decision_trail"], default=str)

            for inst, w in weights_result["weights"].items():
                await conn.execute(
                    """INSERT INTO paper.portfolio_weights
                       (cycle_ts, instrument, weight, method, fallback_reason,
                        portfolio_cvar_95, lookback_days, n_obs, fingerprint, decision_trail)
                       VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)""",
                    cycle_ts,
                    inst,
                    w,
                    weights_result["method"],
                    weights_result["fallback_reason"],
                    weights_result["portfolio_cvar_95"],
                    LOOKBACK_DAYS,
                    weights_result["n_obs"],
                    fingerprint,
                    trail_json,
                )
            for inst, cap in caps.items():
                await conn.execute(
                    """INSERT INTO paper.position_caps
                       (cycle_ts, instrument, tier1_headroom, tier2_atr_cap,
                        tier3_corr_clip, effective_cap_usd, binding_tier, reasons)
                       VALUES ($1,$2,$3,$4,$5,$6,$7,$8)""",
                    cycle_ts,
                    inst,
                    cap["tiers"]["tier1_headroom"],
                    cap["tiers"]["tier2_atr_cap"],
                    cap["tiers"]["tier3_corr_clip"],
                    cap["final_notional"],
                    cap["binding_tier"],
                    json.dumps(cap["reasons"]),
                )
            from paper.risk import log_risk_event

            await log_risk_event(
                conn,
                kind="dynamic_risk_cycle",
                entity_id="cvar_risk_adapter",
                severity="info",
                message=f"weights method={weights_result['method']} n_obs={weights_result['n_obs']}",
                metrics={"fingerprint": fingerprint},
            )
        else:
            from paper.risk import log_risk_event

            await log_risk_event(
                conn,
                kind="dynamic_risk_cycle",
                entity_id="cvar_risk_adapter",
                severity="high",
                message=f"workflow failed: {result['error']}",
                metrics=None,
            )

    return result


async def main(once: bool) -> None:
    hv = await asyncpg.create_pool(HV_DSN, min_size=1, max_size=2)
    try:
        result = await run_once(hv)
        print(f"cvar_risk_adapter: status={result['status']}")
        if result["status"] == "completed":
            print(f"  weights: {result['findings']['weights']}")
    finally:
        await hv.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--once", action="store_true", help="run a single cycle and exit (default)"
    )
    args = parser.parse_args()
    asyncio.run(main(args.once))
