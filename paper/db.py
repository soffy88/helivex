"""paper.db — Schema creation + fill/signal logging for paper trading execution fidelity."""
from __future__ import annotations

import asyncio
import datetime
import json
from typing import Any

import asyncpg

DB_DSN = "postgresql://helios:helios_dev_pass@localhost:5434/helivex"

DDL = """
CREATE SCHEMA IF NOT EXISTS paper;

CREATE TABLE IF NOT EXISTS paper.signals (
    id          BIGSERIAL PRIMARY KEY,
    ts          TIMESTAMPTZ NOT NULL DEFAULT now(),
    strategy_id TEXT        NOT NULL,
    instrument  TEXT        NOT NULL,
    action      TEXT        NOT NULL,
    signal_price DOUBLE PRECISION NOT NULL,
    audit_record_id TEXT,
    fingerprint_hex TEXT,
    sig_b64     TEXT,
    indicators  JSONB
);

-- Migration: add indicators to existing tables that predate this column
ALTER TABLE IF EXISTS paper.signals ADD COLUMN IF NOT EXISTS indicators JSONB;

CREATE TABLE IF NOT EXISTS paper.fills (
    id                  BIGSERIAL PRIMARY KEY,
    ts                  TIMESTAMPTZ NOT NULL DEFAULT now(),
    strategy_id         TEXT        NOT NULL,
    instrument          TEXT        NOT NULL,
    side                TEXT        NOT NULL,
    quantity            DOUBLE PRECISION NOT NULL,
    signal_price        DOUBLE PRECISION,
    actual_fill_price   DOUBLE PRECISION NOT NULL,
    slippage_bps        DOUBLE PRECISION,
    order_id            TEXT,
    venue_order_id      TEXT,
    latency_ms          INTEGER,
    fill_type           TEXT DEFAULT 'taker',
    signal_id           BIGINT REFERENCES paper.signals(id)
);

CREATE TABLE IF NOT EXISTS paper.fidelity_summary (
    id              BIGSERIAL PRIMARY KEY,
    computed_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    strategy_id     TEXT NOT NULL,
    n_signals       INTEGER,
    n_fills         INTEGER,
    fill_rate       DOUBLE PRECISION,
    mean_slippage_bps DOUBLE PRECISION,
    p95_slippage_bps  DOUBLE PRECISION,
    mean_latency_ms   DOUBLE PRECISION
);
"""


async def ensure_schema(conn: asyncpg.Connection) -> None:
    await conn.execute(DDL)


async def log_signal(
    conn: asyncpg.Connection,
    strategy_id: str,
    instrument: str,
    action: str,
    signal_price: float,
    audit_record_id: str = "",
    fingerprint_hex: str = "",
    sig_b64: str = "",
    indicators: dict | None = None,
) -> int:
    row = await conn.fetchrow(
        """INSERT INTO paper.signals
           (strategy_id, instrument, action, signal_price,
            audit_record_id, fingerprint_hex, sig_b64, indicators)
           VALUES ($1,$2,$3,$4,$5,$6,$7,$8)
           RETURNING id""",
        strategy_id, instrument, action, signal_price,
        audit_record_id, fingerprint_hex, sig_b64,
        json.dumps(indicators) if indicators else None,
    )
    return row["id"]


async def log_fill(
    conn: asyncpg.Connection,
    strategy_id: str,
    instrument: str,
    side: str,
    quantity: float,
    signal_price: float | None,
    actual_fill_price: float,
    order_id: str = "",
    venue_order_id: str = "",
    latency_ms: int | None = None,
    fill_type: str = "taker",
    signal_id: int | None = None,
) -> None:
    slippage_bps: float | None = None
    if signal_price and signal_price > 0:
        direction = 1 if side == "BUY" else -1
        slippage_bps = direction * (actual_fill_price - signal_price) / signal_price * 10000

    await conn.execute(
        """INSERT INTO paper.fills
           (strategy_id, instrument, side, quantity,
            signal_price, actual_fill_price, slippage_bps,
            order_id, venue_order_id, latency_ms, fill_type, signal_id)
           VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12)""",
        strategy_id, instrument, side, quantity,
        signal_price, actual_fill_price, slippage_bps,
        order_id, venue_order_id, latency_ms, fill_type, signal_id,
    )


async def fidelity_report(conn: asyncpg.Connection, strategy_id: str = "") -> list[dict]:
    """Compute per-strategy execution fidelity from fill table."""
    where = "WHERE strategy_id = $1" if strategy_id else ""
    args  = [strategy_id] if strategy_id else []
    rows = await conn.fetch(
        f"""SELECT
            strategy_id,
            COUNT(*) FILTER (WHERE id IS NOT NULL) AS n_signals
           FROM paper.signals {where}
           GROUP BY strategy_id""",
        *args,
    )
    fills = await conn.fetch(
        f"""SELECT
            strategy_id,
            COUNT(*) AS n_fills,
            AVG(slippage_bps) AS mean_slippage,
            PERCENTILE_CONT(0.95) WITHIN GROUP (ORDER BY slippage_bps) AS p95_slippage,
            AVG(latency_ms) AS mean_latency
           FROM paper.fills {where}
           GROUP BY strategy_id""",
        *args,
    )
    fills_by_strat = {r["strategy_id"]: r for r in fills}
    results = []
    for sig_row in rows:
        strat = sig_row["strategy_id"]
        f = fills_by_strat.get(strat, {})
        results.append({
            "strategy_id":       strat,
            "n_signals":         sig_row["n_signals"],
            "n_fills":           f.get("n_fills", 0),
            "fill_rate":         f.get("n_fills", 0) / sig_row["n_signals"] if sig_row["n_signals"] else 0,
            "mean_slippage_bps": f.get("mean_slippage"),
            "p95_slippage_bps":  f.get("p95_slippage"),
            "mean_latency_ms":   f.get("mean_latency"),
        })
    return results


async def init_db() -> None:
    conn = await asyncpg.connect(DB_DSN)
    await ensure_schema(conn)
    await conn.close()
    print("[paper.db] schema ensured")


if __name__ == "__main__":
    asyncio.run(init_db())
