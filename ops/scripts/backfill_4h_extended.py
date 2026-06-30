#!/usr/bin/env python3
"""Refresh / backfill 4H OHLCV for BTC/ETH/SOL-USDT-SWAP into market_data.ohlcv_1h
(source='okx_swap').

Two modes:

  --mode forward   (default) Incremental forward refresh: page backward from *now*
                   and stop once we reach the newest bar already stored (the
                   watermark). Idempotent (ON CONFLICT DO NOTHING), so it is safe
                   to run on a 4-hourly timer — steady state fetches 1-2 pages per
                   instrument. This is the path the systemd timer uses to keep the
                   series current (the original backward-only script would never
                   pick up new bars, which left the 4H series frozen).

  --mode history   Original R4.3 behaviour: page BACKWARD from the earliest stored
                   bar as far as OKX history-candles provides (~2019-2021). One-time
                   history extension; not needed once history is in place.

OKX candle ts = bar OPEN time in ms; bar_close_ts stored in DB = open_ts + 4H.
Pagination: `after=open_ts_ms` returns bars with open_ts < this value.
"""
from __future__ import annotations

import argparse
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
TABLE  = "market_data.ohlcv_1h"
SOURCE = "okx_swap"
SLEEP_MS = 80   # ms between API calls — well under 20req/2s limit


async def earliest_open_ts_ms(conn: asyncpg.Connection, inst: str) -> int | None:
    """Return earliest bar open time (= bar_close_ts - 4H) in ms, or None if no rows."""
    row = await conn.fetchrow(
        f"SELECT MIN(bar_close_ts) FROM {TABLE} WHERE instrument=$1 AND source=$2",
        inst, SOURCE,
    )
    if row[0] is None:
        return None
    close_ts: datetime.datetime = row[0]
    open_ts = close_ts - datetime.timedelta(hours=INTERVAL_HOURS)
    return int(open_ts.timestamp() * 1000)


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
    data = r.json().get("data", [])
    return data  # each item: [ts_ms, o, h, l, c, vol, volCcy, volCcyQuote, confirm]


def _rows_from_page(inst: str, page: list[list]) -> list[tuple]:
    """Confirmed bars → insert tuples (sorted ascending by open ts upstream)."""
    rows: list[tuple] = []
    for candle in page:
        confirm = candle[8] if len(candle) > 8 else "1"
        # Only confirmed bars (confirm == "1"); skip the unconfirmed current bar.
        if confirm != "1":
            continue
        open_ts_ms = int(candle[0])
        open_dt  = datetime.datetime.fromtimestamp(open_ts_ms / 1000, tz=datetime.timezone.utc)
        close_dt = open_dt + datetime.timedelta(hours=INTERVAL_HOURS)
        rows.append((
            inst, close_dt, SOURCE,
            float(candle[1]),  # open
            float(candle[2]),  # high
            float(candle[3]),  # low
            float(candle[4]),  # close
            float(candle[5]),  # volume (base)
            float(candle[7]) if len(candle) > 7 else 0.0,  # quote_volume
        ))
    return rows


async def _insert(pool: asyncpg.Pool, rows: list[tuple]) -> None:
    async with pool.acquire() as conn:
        await conn.executemany(
            f"""INSERT INTO {TABLE}
               (instrument, bar_close_ts, source, open, high, low, close, volume, quote_volume)
               VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)
               ON CONFLICT (instrument, bar_close_ts, source) DO NOTHING""",
            rows,
        )


async def refresh_instrument(pool: asyncpg.Pool, client: httpx.AsyncClient,
                             inst: str, default_days: int) -> int:
    """Forward refresh: page back from now, stop at the stored watermark."""
    async with pool.acquire() as conn:
        floor_dt = await watermark_dt(conn, inst, default_days)

    after_ms = None  # start from now (OKX returns newest), page backwards
    inserted = 0; pages = 0

    while True:
        page = await fetch_page(client, inst, after_ms)
        if not page:
            break

        page.sort(key=lambda x: int(x[0]))  # ascending by open ts
        rows = _rows_from_page(inst, page)
        if rows:
            await _insert(pool, rows)
            inserted += len(rows)

        pages += 1
        oldest_open_ms = int(page[0][0])
        oldest_close_dt = datetime.datetime.fromtimestamp(
            oldest_open_ms / 1000, tz=datetime.timezone.utc
        ) + datetime.timedelta(hours=INTERVAL_HOURS)

        if pages % 20 == 0:
            print(f"  {inst}: page {pages}, inserted={inserted}, oldest={oldest_close_dt}")

        # Reached data we already have (or the floor) → stop.
        if oldest_close_dt <= floor_dt:
            break
        if len(page) < 100:   # OKX returned fewer than max → no older data
            break

        after_ms = oldest_open_ms
        await asyncio.sleep(SLEEP_MS / 1000)

    return inserted


async def backfill_history_instrument(pool: asyncpg.Pool, client: httpx.AsyncClient, inst: str) -> int:
    """History mode: page backward from the earliest stored bar to OKX's oldest."""
    async with pool.acquire() as conn:
        after_ms = await earliest_open_ts_ms(conn, inst)

    if after_ms is None:
        print(f"  {inst}: no existing rows — will fetch from latest")

    inserted = 0
    pages = 0

    while True:
        page = await fetch_page(client, inst, after_ms)
        if not page:
            break

        page.sort(key=lambda x: int(x[0]))  # ascending by open ts
        rows_to_insert = _rows_from_page(inst, page)
        if not rows_to_insert:
            break

        await _insert(pool, rows_to_insert)

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


async def main(mode: str, default_days: int) -> None:
    print(f"=== 4H {mode} → {TABLE} (source={SOURCE}) ===\n")

    pool = await asyncpg.create_pool(DB_DSN, min_size=1, max_size=6)
    async with httpx.AsyncClient() as client:
        if mode == "forward":
            results = await asyncio.gather(
                *[refresh_instrument(pool, client, inst, default_days) for inst in INSTRUMENTS],
                return_exceptions=True,
            )
            for inst, result in zip(INSTRUMENTS, results):
                if isinstance(result, Exception):
                    print(f"  {inst}: ERROR — {result}")
                else:
                    print(f"  {inst}: {result} new rows")
        else:  # history
            for inst in INSTRUMENTS:
                print(f"{inst}...")
                n = await backfill_history_instrument(pool, client, inst)
                print(f"  → {n} new rows inserted")

    # Final counts
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
    parser = argparse.ArgumentParser(description="4H OHLCV refresh/backfill into ohlcv_1h (okx_swap)")
    parser.add_argument("--mode", choices=["forward", "history"], default="forward",
                        help="forward = incremental refresh to now (timer); history = extend backward")
    parser.add_argument("--days", type=int, default=30,
                        help="history window to fetch when the table is empty for an instrument (forward mode)")
    args = parser.parse_args()
    asyncio.run(main(args.mode, args.days))
