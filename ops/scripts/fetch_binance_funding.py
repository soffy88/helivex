#!/usr/bin/env python
"""Backfill real Binance historical 8h funding into market_data.binance_funding_history.

Source: Binance USDⓈ-M perp REST  GET /fapi/v1/fundingRate
        params: symbol, startTime, endTime (ms), limit (<=1000).
        Each row is a real settlement point {symbol, fundingTime, fundingRate, markPrice};
        fundingTime is the 0/8/16-UTC settlement instant.

Egress: fapi.binance.com returns 451 from CN. We go through the running SG egress
        container `helios-proxy` (sing-box) — same exit the tape proxy uses. We hit
        Binance directly through it rather than via tape.kanpan.co, because the tape
        proxy exposes only /tape/ticker/funding (premiumIndex = latest, not history).
        Proxy resolution order: --proxy > $BINANCE_FUNDING_PROXY > docker-inspect
        helios-proxy IP:2080 > http://proxy:2080 (the docker-network HTTP_PROXY).

Storage: upsert into market_data.binance_funding_history, PK (symbol, funding_time),
         source='binance_rest' (Binance-only — distinct from the OKX-sourced
         funding in market_data.funding_rates, which holds source='okx').

Usage:
    python ops/scripts/fetch_binance_funding.py                      # full history → now
    python ops/scripts/fetch_binance_funding.py --start 2022-09-01 --end 2022-12-15
"""
from __future__ import annotations

import argparse
import asyncio
import os
import subprocess
from datetime import datetime, timezone

import asyncpg
import httpx

DB_DSN = "postgresql://helios:helios_dev_pass@localhost:5434/helivex"
FAPI_FUNDING = "https://fapi.binance.com/fapi/v1/fundingRate"
BATCH_LIMIT = 1000  # Binance max per call; 8h rate → ~333 days/batch

DDL = """
CREATE TABLE IF NOT EXISTS market_data.binance_funding_history (
    symbol        TEXT             NOT NULL,
    funding_time  TIMESTAMPTZ      NOT NULL,
    funding_rate  DOUBLE PRECISION NOT NULL,
    source        TEXT             NOT NULL DEFAULT 'binance_rest',
    created_at    TIMESTAMPTZ      NOT NULL DEFAULT now(),
    CONSTRAINT binance_funding_history_pk PRIMARY KEY (symbol, funding_time)
);
CREATE INDEX IF NOT EXISTS binance_funding_history_symbol_time
    ON market_data.binance_funding_history (symbol, funding_time DESC);
"""


# ──────────────────── egress ────────────────────

def resolve_proxy(cli_proxy: str | None) -> str:
    if cli_proxy:
        return cli_proxy
    env = os.getenv("BINANCE_FUNDING_PROXY")
    if env:
        return env
    try:
        ip = subprocess.check_output(
            ["docker", "inspect", "helios-proxy",
             "--format", "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}"],
            text=True, stderr=subprocess.DEVNULL, timeout=10,
        ).strip()
        if ip:
            return f"http://{ip}:2080"
    except Exception:
        pass
    return "http://proxy:2080"  # docker-network default ($HTTP_PROXY)


# ──────────────────── fetch + store ────────────────────

def _ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


async def _fetch_batch(client: httpx.AsyncClient, symbol: str,
                       start_ms: int, end_ms: int) -> list[dict]:
    for attempt in range(4):
        try:
            r = await client.get(FAPI_FUNDING, params={
                "symbol": symbol, "startTime": start_ms,
                "endTime": end_ms, "limit": BATCH_LIMIT,
            })
            r.raise_for_status()
            return r.json()
        except Exception as exc:
            wait = 1.5 * (attempt + 1)
            print(f"  [retry {attempt+1}/4] {type(exc).__name__}: {exc} — sleep {wait:.1f}s")
            await asyncio.sleep(wait)
    raise RuntimeError(f"batch failed after retries (startTime={start_ms})")


async def _upsert(conn: asyncpg.Connection, symbol: str, rows: list[dict]) -> None:
    records = [
        (
            symbol,
            datetime.fromtimestamp(int(r["fundingTime"]) / 1000, tz=timezone.utc),
            float(r["fundingRate"]),
            "binance_rest",
        )
        for r in rows
    ]
    await conn.executemany(
        """INSERT INTO market_data.binance_funding_history
               (symbol, funding_time, funding_rate, source)
           VALUES ($1, $2, $3, $4)
           ON CONFLICT (symbol, funding_time)
           DO UPDATE SET funding_rate = EXCLUDED.funding_rate""",
        records,
    )


async def run(args) -> None:
    start = datetime.fromisoformat(args.start).replace(tzinfo=timezone.utc)
    end = (datetime.fromisoformat(args.end).replace(tzinfo=timezone.utc)
           if args.end else datetime.now(timezone.utc))
    proxy = resolve_proxy(args.proxy)
    print(f"Symbol={args.symbol}  window=[{start.date()} → {end.date()}]  egress={proxy}")

    conn = await asyncpg.connect(DB_DSN)
    try:
        await conn.execute(DDL)
        total = 0
        cur_ms, end_ms = _ms(start), _ms(end)
        async with httpx.AsyncClient(proxy=proxy, timeout=20) as client:
            while cur_ms < end_ms:
                batch = await _fetch_batch(client, args.symbol, cur_ms, end_ms)
                if not batch:
                    break
                await _upsert(conn, args.symbol, batch)
                last_ms = int(batch[-1]["fundingTime"])
                total += len(batch)
                first_dt = datetime.fromtimestamp(int(batch[0]["fundingTime"]) / 1000, tz=timezone.utc)
                last_dt = datetime.fromtimestamp(last_ms / 1000, tz=timezone.utc)
                print(f"  +{len(batch):4d}  [{first_dt:%Y-%m-%d} → {last_dt:%Y-%m-%d}]  total={total}")
                cur_ms = last_ms + 1
                if len(batch) < BATCH_LIMIT:
                    break  # fewer than a full page → caught up
                await asyncio.sleep(args.sleep)
        print(f"Done. Upserted {total} settlement rows.")

        # ── validation ──
        row = await conn.fetchrow(
            """SELECT MIN(funding_time) lo, MAX(funding_time) hi, COUNT(*) n
               FROM market_data.binance_funding_history WHERE symbol=$1""",
            args.symbol,
        )
        print(f"\nVALIDATION  earliest={row['lo']}  latest={row['hi']}  count={row['n']}")
        ftx = await conn.fetch(
            """SELECT funding_time, funding_rate
               FROM market_data.binance_funding_history
               WHERE symbol=$1 AND funding_time >= '2022-11-08' AND funding_time < '2022-11-10'
               ORDER BY funding_time""",
            args.symbol,
        )
        print(f"FTX 2022-11-08/09 ({len(ftx)} rows) — expect some deeply NEGATIVE (panic):")
        for r in ftx:
            flag = "  <-- negative" if r["funding_rate"] < 0 else ""
            print(f"  {r['funding_time']:%Y-%m-%d %H:%M}  {r['funding_rate']:+.6f}{flag}")
    finally:
        await conn.close()


def main() -> None:
    p = argparse.ArgumentParser(description="Backfill Binance historical 8h funding")
    p.add_argument("--symbol", default="BTCUSDT")
    p.add_argument("--start", default="2019-09-01", help="ISO date UTC (perp listed ~2019-09-10)")
    p.add_argument("--end", default="", help="ISO date UTC; default = now")
    p.add_argument("--proxy", default="", help="override egress proxy URL")
    p.add_argument("--sleep", type=float, default=0.4, help="seconds between batches (rate limit)")
    args = p.parse_args()
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
