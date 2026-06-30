#!/usr/bin/env python3
"""
R5.1 / Task-11: Refresh 5m OHLCV for BTC/ETH/SOL-USDT-SWAP into market_data.ohlcv_5m.

Incremental forward refresh: page backward from *now* and stop once we reach the
newest bar already stored (the watermark). On a fresh/empty table it pages back
to `--days` (default 30) of history. Idempotent (ON CONFLICT DO NOTHING), so it is
safe to run on a short timer — steady state fetches 1-2 pages per instrument.

OKX candle ts = bar OPEN time (ms); bar_close_ts stored = open_ts + 5m.
Runs 3 instruments in parallel to reduce wall-clock time.
"""
from __future__ import annotations

import argparse
import asyncio
import datetime

import asyncpg
import httpx

DB_DSN          = "postgresql://helios:helios_dev_pass@localhost:5434/helivex"
OKX_BASE        = "https://www.okx.com"
INSTRUMENTS     = ["BTC-USDT-SWAP", "ETH-USDT-SWAP", "SOL-USDT-SWAP"]
BAR_INTERVAL    = "5m"
INTERVAL_MINUTES = 5
TABLE           = "market_data.ohlcv_5m"
SOURCE          = "okx_swap_5m"   # kept for continuity with migrated history
SLEEP_MS        = 80   # ms between requests per instrument; 3 parallel ≈ 12 req/s total


async def watermark_dt(conn: asyncpg.Connection, inst: str, default_days: int) -> datetime.datetime:
    """Newest bar_close_ts already stored for `inst`, or (now - default_days) if empty."""
    row = await conn.fetchrow(
        f"SELECT MAX(bar_close_ts) FROM {TABLE} WHERE instrument=$1 AND source=$2",
        inst, SOURCE,
    )
    if row[0] is not None:
        return row[0]
    return datetime.datetime.now(tz=datetime.timezone.utc) - datetime.timedelta(days=default_days)


async def fetch_page(client: httpx.AsyncClient, inst: str, after_ms: int | None) -> list[list]:
    params: dict = {"instId": inst, "bar": BAR_INTERVAL, "limit": "100"}
    if after_ms is not None:
        params["after"] = str(after_ms)
    r = await client.get(OKX_BASE + "/api/v5/market/history-candles", params=params, timeout=30)
    return r.json().get("data", [])


async def backfill_instrument(pool: asyncpg.Pool, client: httpx.AsyncClient,
                              inst: str, default_days: int) -> int:
    async with pool.acquire() as conn:
        floor_dt = await watermark_dt(conn, inst, default_days)

    after_ms = None  # start from now (OKX returns newest), page backwards
    inserted = 0; pages = 0

    while True:
        page = await fetch_page(client, inst, after_ms)
        if not page:
            break

        page.sort(key=lambda x: int(x[0]))  # ascending by open ts
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

        if rows:
            async with pool.acquire() as conn:
                await conn.executemany(
                    f"""INSERT INTO {TABLE}
                       (instrument, bar_close_ts, source, open, high, low, close, volume, quote_volume)
                       VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)
                       ON CONFLICT (instrument, bar_close_ts, source) DO NOTHING""",
                    rows,
                )
            inserted += len(rows)

        pages += 1
        oldest_open_ms = int(page[0][0])
        oldest_close_dt = datetime.datetime.fromtimestamp(
            oldest_open_ms / 1000, tz=datetime.timezone.utc
        ) + datetime.timedelta(minutes=INTERVAL_MINUTES)

        if pages % 50 == 0:
            print(f"  {inst}: page {pages}, inserted={inserted}, oldest={oldest_close_dt}")

        # Reached data we already have (or the floor) → stop.
        if oldest_close_dt <= floor_dt:
            break
        if len(page) < 100:   # OKX returned fewer than max → no older data
            break

        after_ms = oldest_open_ms
        await asyncio.sleep(SLEEP_MS / 1000)

    return inserted


async def main(default_days: int) -> None:
    print(f"=== 5m refresh → {TABLE} (incremental, fallback {default_days}d) ===\n")
    pool = await asyncpg.create_pool(DB_DSN, min_size=1, max_size=6)

    async with httpx.AsyncClient() as client:
        results = await asyncio.gather(
            *[backfill_instrument(pool, client, inst, default_days) for inst in INSTRUMENTS],
            return_exceptions=True,
        )

    for inst, result in zip(INSTRUMENTS, results):
        if isinstance(result, Exception):
            print(f"  {inst}: ERROR — {result}")
        else:
            print(f"  {inst}: {result} new rows")

    print("\nFinal counts:")
    async with pool.acquire() as conn:
        for inst in INSTRUMENTS:
            row = await conn.fetchrow(
                f"""SELECT COUNT(*), MIN(bar_close_ts), MAX(bar_close_ts)
                   FROM {TABLE} WHERE instrument=$1 AND source=$2""",
                inst, SOURCE,
            )
            if row[0]:
                print(f"  {inst}: {row[0]} bars  {row[1]} → {row[2]}")
            else:
                print(f"  {inst}: 0 bars")

    await pool.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Incremental 5m OHLCV refresh into ohlcv_5m")
    parser.add_argument("--days", type=int, default=30,
                        help="history window to fetch when the table is empty for an instrument")
    args = parser.parse_args()
    asyncio.run(main(args.days))
