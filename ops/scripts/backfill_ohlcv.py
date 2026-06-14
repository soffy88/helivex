"""Backfill market_data.ohlcv_1h and funding_rates from OKX public API.

Usage:
    python ops/scripts/backfill_ohlcv.py [--months 12]

No OKX API key required — uses public history-candles endpoint.
Idempotent (ON CONFLICT DO NOTHING).
"""
from __future__ import annotations

import argparse
import asyncio
import time
from datetime import datetime, timedelta, timezone

import asyncpg
import httpx

DB_DSN = "postgresql://helios:helios_dev_pass@localhost:5434/helivex"
OKX_BASE = "https://www.okx.com"
INSTRUMENTS = ["BTC-USDT", "ETH-USDT", "SOL-USDT"]
SWAP_INSTRUMENTS = ["BTC-USDT-SWAP", "ETH-USDT-SWAP", "SOL-USDT-SWAP"]


async def fetch_candles(
    client: httpx.AsyncClient,
    inst_id: str,
    bar: str,
    after_ms: int | None,
    limit: int = 100,
) -> list[list]:
    """Fetch up to `limit` candles before `after_ms` (ms epoch)."""
    params: dict = {"instId": inst_id, "bar": bar, "limit": str(limit)}
    if after_ms is not None:
        params["after"] = str(after_ms)

    # Try history-candles first (>1 month old), fall back to candles
    for endpoint in ["/api/v5/market/history-candles", "/api/v5/market/candles"]:
        try:
            r = await client.get(OKX_BASE + endpoint, params=params, timeout=15)
            data = r.json()
            if str(data.get("code", "1")) == "0":
                return data.get("data", [])
        except Exception:
            pass
    return []


async def backfill_ohlcv(
    conn: asyncpg.Connection,
    client: httpx.AsyncClient,
    inst_id: str,
    months: int,
) -> int:
    """Backfill ohlcv_1h for one instrument. Returns rows inserted."""
    cutoff_ms = int(
        (datetime.now(timezone.utc) - timedelta(days=30 * months)).timestamp() * 1000
    )
    rows_inserted = 0
    after_ms: int | None = None  # start from now, page backwards

    while True:
        candles = await fetch_candles(client, inst_id, "1H", after_ms)
        if not candles:
            break

        # OKX returns newest-first; oldest bar timestamp to page backwards
        oldest_ts = int(candles[-1][0])

        records = []
        for row in candles:
            ts_ms = int(row[0])
            if ts_ms < cutoff_ms:
                continue
            bar_close_ts = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)
            records.append((
                inst_id,
                bar_close_ts,
                "okx",
                float(row[1]),  # open
                float(row[2]),  # high
                float(row[3]),  # low
                float(row[4]),  # close
                float(row[5]),  # volume
                float(row[6]) if len(row) > 6 else None,  # quote_volume
            ))

        if records:
            result = await conn.executemany(
                """
                INSERT INTO market_data.ohlcv_1h
                    (instrument, bar_close_ts, source, open, high, low, close, volume, quote_volume)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
                ON CONFLICT (instrument, bar_close_ts, source) DO NOTHING
                """,
                records,
            )
            inserted = int(result.split()[-1]) if result else 0
            rows_inserted += inserted

        if oldest_ts <= cutoff_ms:
            break

        after_ms = oldest_ts
        await asyncio.sleep(0.12)  # ~8 req/s — stay well under OKX rate limit

    return rows_inserted


async def backfill_funding(
    conn: asyncpg.Connection,
    client: httpx.AsyncClient,
    swap_id: str,
) -> int:
    """Backfill funding_rates for one SWAP instrument (full history). Returns rows inserted."""
    total_inserted = 0
    after_ms: int | None = None  # paginate backwards using 'after'

    while True:
        params: dict = {"instId": swap_id, "limit": "100"}
        if after_ms is not None:
            params["after"] = str(after_ms)

        r = await client.get(
            OKX_BASE + "/api/v5/public/funding-rate-history",
            params=params,
            timeout=15,
        )
        data = r.json()
        if str(data.get("code", "1")) != "0":
            print(f"  [funding] {swap_id}: API error {data.get('code')} {data.get('msg')}")
            break

        items = data.get("data", [])
        if not items:
            break

        # OKX returns newest-first; use oldest ts to page further back
        oldest_ms = int(items[-1]["fundingTime"])

        records = []
        for row in items:
            ts = datetime.fromtimestamp(int(row["fundingTime"]) / 1000, tz=timezone.utc)
            next_ts = (
                datetime.fromtimestamp(int(row["nextFundingTime"]) / 1000, tz=timezone.utc)
                if row.get("nextFundingTime")
                else None
            )
            records.append((
                swap_id,
                ts,
                "okx",
                float(row.get("fundingRate", 0)),
                float(row.get("realizedRate", row.get("fundingRate", 0))),
                next_ts,
            ))

        if records:
            result = await conn.executemany(
                """
                INSERT INTO market_data.funding_rates
                    (instrument, ts, source, funding_rate, realized_rate, next_funding_time)
                VALUES ($1, $2, $3, $4, $5, $6)
                ON CONFLICT (instrument, ts, source) DO NOTHING
                """,
                records,
            )
            inserted = int(result.split()[-1]) if result else 0
            total_inserted += inserted

        if len(items) < 100:
            break  # last page

        after_ms = oldest_ms
        await asyncio.sleep(0.12)

    return total_inserted


async def main(months: int = 12) -> None:
    conn = await asyncpg.connect(DB_DSN)
    headers = {"User-Agent": "helivex-backfill/1.0"}

    async with httpx.AsyncClient(headers=headers) as client:
        print(f"=== OHLCV 1H backfill — {months} months back ===")
        total_ohlcv = 0
        for inst in INSTRUMENTS:
            t0 = time.monotonic()
            n = await backfill_ohlcv(conn, client, inst, months)
            total_ohlcv += n
            print(f"  {inst:15s}  {n:5d} rows  ({time.monotonic()-t0:.1f}s)")

        print(f"\n=== Funding rate backfill (full available history) ===")
        total_funding = 0
        for swap in SWAP_INSTRUMENTS:
            n = await backfill_funding(conn, client, swap)
            total_funding += n
            print(f"  {swap:20s}  {n:4d} rows")

        # Row count summary
        print("\n=== DB row counts ===")
        for inst in INSTRUMENTS:
            cnt = await conn.fetchval(
                "SELECT COUNT(*) FROM market_data.ohlcv_1h WHERE instrument = $1", inst
            )
            print(f"  ohlcv_1h  {inst:15s}  {cnt} rows")
        for swap in SWAP_INSTRUMENTS:
            cnt = await conn.fetchval(
                "SELECT COUNT(*) FROM market_data.funding_rates WHERE instrument = $1", swap
            )
            print(f"  funding   {swap:20s}  {cnt} rows")

        span = await conn.fetchrow(
            "SELECT MIN(bar_close_ts), MAX(bar_close_ts) FROM market_data.ohlcv_1h"
        )
        print(f"\n  Date range: {span['min']} → {span['max']}")

    await conn.close()
    print(f"\nTotal ohlcv rows inserted this run: {total_ohlcv}")
    print(f"Total funding rows inserted this run: {total_funding}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--months", type=int, default=12)
    args = parser.parse_args()
    asyncio.run(main(args.months))
