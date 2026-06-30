#!/usr/bin/env python3
"""Read-only data-quality diagnostics for market_data.

  python ops/scripts/data_quality.py

Reports, per (instrument, source):
  - OHLCV gaps: bars missing vs the expected interval over the covered span.
  - Funding reconciliation: where funding_rates (okx) and binance_funding_history
    overlap in time, how far apart the rates are (provenance sanity check).

Pure SELECTs — never writes. Wire into the monitor later if you want alerting.
"""
from __future__ import annotations

import asyncio

import asyncpg

DB_DSN = "postgresql://helios:helios_dev_pass@localhost:5434/helivex"

# expected bar interval (seconds) per table
TABLES = {"market_data.ohlcv_1h": 3600, "market_data.ohlcv_5m": 300}


async def ohlcv_gaps(conn) -> None:
    print("── OHLCV gaps (missing bars vs expected interval) ──────────────")
    for tbl, step in TABLES.items():
        rows = await conn.fetch(
            f"""
            WITH d AS (
              SELECT instrument, source, bar_close_ts,
                     EXTRACT(EPOCH FROM bar_close_ts
                        - lag(bar_close_ts) OVER (PARTITION BY instrument, source
                                                  ORDER BY bar_close_ts)) AS gap_s
              FROM {tbl}
            )
            SELECT instrument, source,
                   COUNT(*) FILTER (WHERE gap_s > {step} * 1.5) AS gaps,
                   COALESCE(SUM((gap_s/{step})::bigint - 1)
                            FILTER (WHERE gap_s > {step} * 1.5), 0) AS missing_bars
            FROM d GROUP BY instrument, source
            HAVING COUNT(*) FILTER (WHERE gap_s > {step} * 1.5) > 0
            ORDER BY missing_bars DESC LIMIT 15
            """
        )
        print(f"\n  {tbl}:")
        if not rows:
            print("    no gaps")
        for r in rows:
            print(f"    {r['instrument']:<22} src={r['source']:<14} "
                  f"gaps={r['gaps']:<5} missing_bars={r['missing_bars']}")


async def funding_reconcile(conn) -> None:
    print("\n── Funding reconciliation (okx funding_rates vs binance_funding_history) ──")
    row = await conn.fetchrow(
        """
        WITH ovl AS (
          SELECT date_trunc('hour', f.ts) AS h, AVG(f.funding_rate) AS okx_rate
          FROM market_data.funding_rates f WHERE f.instrument ILIKE 'BTC%'
          GROUP BY 1
        ), bin AS (
          SELECT date_trunc('hour', funding_time) AS h, AVG(funding_rate) AS bin_rate
          FROM market_data.binance_funding_history WHERE symbol = 'BTCUSDT'
          GROUP BY 1
        )
        SELECT COUNT(*) AS n,
               AVG(ABS(ovl.okx_rate - bin.bin_rate)) AS mean_abs_diff,
               MAX(ABS(ovl.okx_rate - bin.bin_rate)) AS max_abs_diff
        FROM ovl JOIN bin USING (h)
        """
    )
    if row and row["n"]:
        print(f"  BTC overlap hours={row['n']}  mean|okx-binance|={row['mean_abs_diff']:.6g}  "
              f"max={row['max_abs_diff']:.6g}")
    else:
        print("  no overlapping BTC funding hours to reconcile")


async def main() -> None:
    conn = await asyncpg.connect(DB_DSN)
    try:
        await ohlcv_gaps(conn)
        await funding_reconcile(conn)
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
