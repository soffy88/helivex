#!/usr/bin/env python3
"""
R5.1: Backfill 5m OHLCV for BTC/ETH/SOL-USDT-SWAP.
OKX history-candles for 5m goes back to Dec 2019 (same depth as 4H).
Runs 3 instruments in parallel to reduce total wall-clock time.
"""
from __future__ import annotations

import asyncio
import datetime
import sys

import asyncpg
import httpx

DB_DSN          = "postgresql://helios:helios_dev_pass@localhost:5434/helivex"
OKX_BASE        = "https://www.okx.com"
INSTRUMENTS     = ["BTC-USDT-SWAP", "ETH-USDT-SWAP", "SOL-USDT-SWAP"]
BAR_INTERVAL    = "5m"
INTERVAL_MINUTES = 5
SOURCE          = "okx_swap_5m"
SLEEP_MS        = 80   # ms between requests per instrument; 3 parallel ≈ 12 req/s total


async def earliest_open_ts_ms(conn: asyncpg.Connection, inst: str) -> int | None:
    row = await conn.fetchrow(
        "SELECT MIN(bar_close_ts) FROM market_data.ohlcv_1h WHERE instrument=$1 AND source=$2",
        inst, SOURCE,
    )
    if row[0] is None:
        return None
    open_ts = row[0] - datetime.timedelta(minutes=INTERVAL_MINUTES)
    return int(open_ts.timestamp() * 1000)


async def fetch_page(client: httpx.AsyncClient, inst: str, after_ms: int | None) -> list[list]:
    params: dict = {"instId": inst, "bar": BAR_INTERVAL, "limit": "100"}
    if after_ms is not None:
        params["after"] = str(after_ms)
    r = await client.get(OKX_BASE + "/api/v5/market/history-candles", params=params, timeout=30)
    return r.json().get("data", [])


async def backfill_instrument(pool: asyncpg.Pool, client: httpx.AsyncClient, inst: str) -> int:
    async with pool.acquire() as conn:
        after_ms = await earliest_open_ts_ms(conn, inst)

    inserted = 0; pages = 0

    while True:
        page = await fetch_page(client, inst, after_ms)
        if not page:
            break

        page.sort(key=lambda x: int(x[0]))
        rows: list[tuple] = []
        for candle in page:
            confirm = candle[8] if len(candle) > 8 else "1"
            if confirm != "1":
                continue
            open_ts_ms = int(candle[0])
            open_dt    = datetime.datetime.fromtimestamp(open_ts_ms / 1000, tz=datetime.timezone.utc)
            close_dt   = open_dt + datetime.timedelta(minutes=INTERVAL_MINUTES)
            rows.append((
                inst, close_dt, SOURCE,
                float(candle[1]),  # open
                float(candle[2]),  # high
                float(candle[3]),  # low
                float(candle[4]),  # close
                float(candle[5]),  # volume (base)
                float(candle[7]) if len(candle) > 7 else 0.0,  # quote volume
            ))

        if not rows:
            break

        async with pool.acquire() as conn:
            await conn.executemany(
                """INSERT INTO market_data.ohlcv_1h
                   (instrument, bar_close_ts, source, open, high, low, close, volume, quote_volume)
                   VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)
                   ON CONFLICT (instrument, bar_close_ts, source) DO NOTHING""",
                rows,
            )

        n_new     = len(rows)
        inserted += n_new
        pages    += 1
        oldest_ms = int(page[0][0])
        after_ms  = oldest_ms

        if pages % 100 == 0 or n_new < 100:
            oldest_dt = datetime.datetime.fromtimestamp(oldest_ms / 1000, tz=datetime.timezone.utc)
            print(f"  {inst}: page {pages}, inserted={inserted}, oldest={oldest_dt.date()}")

        if len(page) < 100:   # OKX returned fewer than max → oldest data reached
            break

        await asyncio.sleep(SLEEP_MS / 1000)

    return inserted


async def main() -> None:
    print("=== R5.1: 5m backfill (parallel) ===\n")
    pool = await asyncpg.create_pool(DB_DSN, min_size=1, max_size=6)

    async with httpx.AsyncClient() as client:
        results = await asyncio.gather(
            *[backfill_instrument(pool, client, inst) for inst in INSTRUMENTS],
            return_exceptions=True,
        )

    for inst, result in zip(INSTRUMENTS, results):
        if isinstance(result, Exception):
            print(f"  {inst}: ERROR — {result}")
        else:
            print(f"  {inst}: {result} new rows")

    # Final counts
    print("\nFinal counts:")
    async with pool.acquire() as conn:
        for inst in INSTRUMENTS:
            row = await conn.fetchrow(
                """SELECT COUNT(*), MIN(bar_close_ts), MAX(bar_close_ts)
                   FROM market_data.ohlcv_1h WHERE instrument=$1 AND source=$2""",
                inst, SOURCE,
            )
            if row[0]:
                print(f"  {inst}: {row[0]} bars  {row[1].date()} → {row[2].date()}")
            else:
                print(f"  {inst}: 0 bars")

    await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
