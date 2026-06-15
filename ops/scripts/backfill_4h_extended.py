#!/usr/bin/env python3
"""
R4.3: Extend 4H OHLCV history backward as far as OKX history-candles provides.
Current DB has BTC/ETH/SOL-USDT-SWAP starting 2025-06-20.
Target: fetch all available history (~2019-2021 depending on instrument).

OKX candle ts = bar OPEN time in ms.
bar_close_ts stored in DB = open_ts + 4H.
Pagination: `after=open_ts_ms` returns bars with open_ts < this value.
"""
from __future__ import annotations

import asyncio
import datetime
import sys

import asyncpg
import httpx

DB_DSN   = "postgresql://helios:helios_dev_pass@localhost:5434/helivex"
OKX_BASE = "https://www.okx.com"
INSTRUMENTS = ["BTC-USDT-SWAP", "ETH-USDT-SWAP", "SOL-USDT-SWAP"]
BAR_INTERVAL = "4H"
INTERVAL_HOURS = 4
SLEEP_MS = 80   # ms between API calls — well under 20req/2s limit


async def earliest_open_ts_ms(conn: asyncpg.Connection, inst: str) -> int | None:
    """Return earliest bar open time (= bar_close_ts - 4H) in ms, or None if no rows."""
    row = await conn.fetchrow(
        "SELECT MIN(bar_close_ts) FROM market_data.ohlcv_1h WHERE instrument=$1 AND source='okx_swap'",
        inst,
    )
    if row[0] is None:
        return None
    close_ts: datetime.datetime = row[0]
    open_ts = close_ts - datetime.timedelta(hours=INTERVAL_HOURS)
    return int(open_ts.timestamp() * 1000)


async def fetch_page(client: httpx.AsyncClient, inst: str, after_ms: int | None) -> list[list]:
    params: dict = {"instId": inst, "bar": BAR_INTERVAL, "limit": "100"}
    if after_ms is not None:
        params["after"] = str(after_ms)
    r = await client.get(OKX_BASE + "/api/v5/market/history-candles", params=params, timeout=20)
    data = r.json().get("data", [])
    return data  # each item: [ts_ms, o, h, l, c, vol, volCcy, volCcyQuote, confirm]


async def backfill_instrument(pool: asyncpg.Pool, client: httpx.AsyncClient, inst: str) -> int:
    async with pool.acquire() as conn:
        after_ms = await earliest_open_ts_ms(conn, inst)

    if after_ms is None:
        print(f"  {inst}: no existing rows — will fetch from latest")
        after_ms = None

    inserted = 0
    pages = 0

    while True:
        page = await fetch_page(client, inst, after_ms)
        if not page:
            break

        # Sort ascending by open ts
        page.sort(key=lambda x: int(x[0]))
        rows_to_insert = []
        for candle in page:
            open_ts_ms = int(candle[0])
            confirm    = candle[8] if len(candle) > 8 else "1"
            # Only confirmed bars (confirm == "1"); skip unconfirmed current bar
            if confirm != "1":
                continue
            open_dt  = datetime.datetime.fromtimestamp(open_ts_ms / 1000, tz=datetime.timezone.utc)
            close_dt = open_dt + datetime.timedelta(hours=INTERVAL_HOURS)
            rows_to_insert.append((
                inst,
                close_dt,
                "okx_swap",
                float(candle[1]),  # open
                float(candle[2]),  # high
                float(candle[3]),  # low
                float(candle[4]),  # close
                float(candle[5]),  # volume (base)
                float(candle[7]) if len(candle) > 7 else 0.0,  # quote_volume
            ))

        if not rows_to_insert:
            break

        async with pool.acquire() as conn:
            result = await conn.executemany(
                """INSERT INTO market_data.ohlcv_1h
                   (instrument, bar_close_ts, source, open, high, low, close, volume, quote_volume)
                   VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)
                   ON CONFLICT (instrument, bar_close_ts, source) DO NOTHING""",
                rows_to_insert,
            )

        n_new = len(rows_to_insert)
        inserted += n_new
        pages += 1

        # Oldest open ts in this page — paginate further back
        oldest_open_ms = int(page[0][0])
        after_ms = oldest_open_ms  # next page: bars older than this

        oldest_dt = datetime.datetime.fromtimestamp(oldest_open_ms / 1000, tz=datetime.timezone.utc)
        if pages % 20 == 0 or n_new < 100:
            print(f"    page {pages}: {n_new} rows, oldest={oldest_dt.date()}, total_inserted={inserted}")

        if n_new < 100:
            break  # reached oldest available data

        await asyncio.sleep(SLEEP_MS / 1000)

    return inserted


async def main() -> None:
    print("=== R4.3: Extended 4H backfill ===\n")

    pool = await asyncpg.create_pool(DB_DSN, min_size=1, max_size=3)
    async with httpx.AsyncClient() as client:
        for inst in INSTRUMENTS:
            print(f"{inst}...")
            n = await backfill_instrument(pool, client, inst)
            print(f"  → {n} new rows inserted")

    # Final counts
    async with pool.acquire() as conn:
        for inst in INSTRUMENTS:
            row = await conn.fetchrow(
                """SELECT COUNT(*), MIN(bar_close_ts), MAX(bar_close_ts)
                   FROM market_data.ohlcv_1h WHERE instrument=$1 AND source='okx_swap'""",
                inst,
            )
            print(f"  {inst}: {row[0]} bars  {row[1].date()} → {row[2].date()}")

    await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
