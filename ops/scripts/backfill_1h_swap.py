#!/usr/bin/env python3
"""Backfill TRUE 1H OHLCV for BTC/ETH/SOL-USDT-SWAP into market_data.ohlcv_1h
(source='okx_swap_1h').

背景:ohlcv_1h 里 SWAP 的既有行(source='okx_swap')实际是 4H bar(backfill_4h_extended
的产物)——表里此前不存在真 1H SWAP 序列。futures_signal_port(1H 策略)的 warmup/gate
需要真 1H 采样,故新增独立 source 回填,不动既有 4H 行(PK 含 source,干净共存)。

Pages backward from *now* to --months cutoff regardless of watermark (ON CONFLICT
DO NOTHING → idempotent, re-runs only fill gaps). OKX candle ts = bar OPEN time (ms);
bar_close_ts stored = open_ts + 1H (与 4h/5m 脚本同一约定).

Usage:
    python ops/scripts/backfill_1h_swap.py [--months 24]
"""

from __future__ import annotations

import argparse
import asyncio
import datetime

import asyncpg
import httpx

DB_DSN = "postgresql://helios:helios_dev_pass@localhost:5434/helivex"
OKX_BASE = "https://www.okx.com"
INSTRUMENTS = ["BTC-USDT-SWAP", "ETH-USDT-SWAP", "SOL-USDT-SWAP"]
SOURCE = "okx_swap_1h"
SLEEP_S = 0.08  # sequential instruments → ~12 req/s, well under OKX 20 req/2s


async def fetch_page(
    client: httpx.AsyncClient, inst: str, after_ms: int | None
) -> list[list]:
    params: dict = {"instId": inst, "bar": "1H", "limit": "100"}
    if after_ms is not None:
        params["after"] = str(after_ms)
    for endpoint in ("/api/v5/market/history-candles", "/api/v5/market/candles"):
        try:
            r = await client.get(OKX_BASE + endpoint, params=params, timeout=15)
            data = r.json()
            if str(data.get("code", "1")) == "0" and data.get("data"):
                return data["data"]
        except Exception:
            pass
    return []


async def backfill(
    conn: asyncpg.Connection, client: httpx.AsyncClient, inst: str, months: int
) -> int:
    cutoff_ms = int(
        (
            datetime.datetime.now(datetime.timezone.utc)
            - datetime.timedelta(days=30 * months)
        ).timestamp()
        * 1000
    )
    inserted = 0
    after_ms: int | None = None
    empty_streak = 0
    while True:
        candles = await fetch_page(client, inst, after_ms)
        if not candles:
            empty_streak += 1
            if empty_streak >= 3:  # transient rate-limit tolerance, then give up
                break
            await asyncio.sleep(1.5)
            continue
        empty_streak = 0
        oldest_open_ms = int(candles[-1][0])
        records = []
        for row in candles:
            open_ms = int(row[0])
            if open_ms < cutoff_ms:
                continue
            close_ts = datetime.datetime.fromtimestamp(
                open_ms / 1000, tz=datetime.timezone.utc
            ) + datetime.timedelta(hours=1)
            records.append(
                (
                    inst,
                    close_ts,
                    SOURCE,
                    float(row[1]),
                    float(row[2]),
                    float(row[3]),
                    float(row[4]),
                    float(row[5]),
                    float(row[6]) if len(row) > 6 else None,
                )
            )
        if records:
            await conn.executemany(
                """INSERT INTO market_data.ohlcv_1h
                       (instrument, bar_close_ts, source, open, high, low, close, volume, quote_volume)
                   VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)
                   ON CONFLICT (instrument, bar_close_ts, source) DO NOTHING""",
                records,
            )
            inserted += len(records)
        if oldest_open_ms <= cutoff_ms:
            break
        after_ms = oldest_open_ms
        await asyncio.sleep(SLEEP_S)
    return inserted


async def main(months: int) -> None:
    conn = await asyncpg.connect(DB_DSN)
    try:
        async with httpx.AsyncClient() as client:
            for inst in INSTRUMENTS:
                n = await backfill(conn, client, inst, months)
                print(f"{inst}: ~{n} 1H rows upserted (source={SOURCE})")
    finally:
        await conn.close()


if __name__ == "__main__":
    p = argparse.ArgumentParser(
        description="Backfill true 1H SWAP OHLCV (source=okx_swap_1h)"
    )
    p.add_argument("--months", type=int, default=24)
    args = p.parse_args()
    asyncio.run(main(args.months))
