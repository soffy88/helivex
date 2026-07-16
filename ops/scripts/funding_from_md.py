"""Funding adapter — 从统一采集层 iris 的 md.funding_settled 回填 helivex 的 funding 表
(采集层合并, 承接 OHLCV P2a 的同一模式)。

背景: helivex 原本自采两路 funding —— OKX(market_data.funding_rates) 与 Binance
(market_data.binance_funding_history, fetch_binance_funding.py)。iris/md 的
iris_funding_settled_{okx,binance} 采集器已中立生产同一份数据到 `md.funding_settled`
(两 venue, 逐项对账值一致: okx rate/realized_rate 字节级一致)。本 adapter 纯 DB→DB
(md 与 helivex 同在 platform-postgres localhost:5434), **无 egress、不需代理** ——
比原 REST ingest 更简单可靠, 且让 Binance funding(此前自采空表)重新有数据。

口径映射(md.funding_settled → helivex):
  - okx     → market_data.funding_rates: instrument=md.inst_id(BTC-USDT-SWAP), ts=funding_time,
              source='okx', funding_rate/realized_rate/next_funding_time 直传
  - binance → market_data.binance_funding_history: symbol=md.inst_id(BTCUSDT), funding_time,
              funding_rate, source='binance_rest'(消费方 run_features 只按 symbol 过滤, source 不影响)
  - ON CONFLICT DO NOTHING —— 幂等、深史不动(helivex okx funding_rates 现有 902 行深史保留,
    md 只补新; binance 现为空, 由 md 填充)

回滚: 重新 enable helivex-ingest-funding-binance.timer、disable 本 adapter timer(旧脚本保留)。
"""

from __future__ import annotations

import asyncio
import os

import asyncpg

MD_DSN = os.environ.get(
    "MD_DSN", "postgresql://helios:helios_dev_pass@localhost:5434/marketdata"
)
HV_DSN = os.environ.get(
    "DB_DSN", "postgresql://helios:helios_dev_pass@localhost:5434/helivex"
)


async def sync_okx(md: asyncpg.Pool, hv: asyncpg.Pool) -> int:
    rows = await md.fetch(
        """
        SELECT inst_id, funding_time, funding_rate, realized_rate, next_funding_time
        FROM md.funding_settled
        WHERE venue='okx' AND inst_id IS NOT NULL
        """
    )
    n = 0
    for r in rows:
        await hv.execute(
            """
            INSERT INTO market_data.funding_rates
                (instrument, ts, source, funding_rate, realized_rate, next_funding_time)
            VALUES ($1,$2,'okx',$3,$4,$5)
            ON CONFLICT (instrument, ts, source) DO NOTHING
            """,
            r["inst_id"],
            r["funding_time"],
            r["funding_rate"],
            r["realized_rate"],
            r["next_funding_time"],
        )
        n += 1
    return n


async def sync_binance(md: asyncpg.Pool, hv: asyncpg.Pool) -> int:
    rows = await md.fetch(
        """
        SELECT inst_id, funding_time, funding_rate
        FROM md.funding_settled
        WHERE venue='binance' AND inst_id IS NOT NULL
        """
    )
    n = 0
    for r in rows:
        await hv.execute(
            """
            INSERT INTO market_data.binance_funding_history
                (symbol, funding_time, funding_rate, source)
            VALUES ($1,$2,$3,'binance_rest')
            ON CONFLICT (symbol, funding_time) DO NOTHING
            """,
            r["inst_id"],
            r["funding_time"],
            float(r["funding_rate"]),
        )
        n += 1
    return n


async def main() -> None:
    md = await asyncpg.create_pool(MD_DSN, min_size=1, max_size=2)
    hv = await asyncpg.create_pool(HV_DSN, min_size=1, max_size=2)
    try:
        n_okx = await sync_okx(md, hv)
        n_bin = await sync_binance(md, hv)
        print(f"funding_from_md: okx {n_okx} rows upserted -> funding_rates")
        print(
            f"funding_from_md: binance {n_bin} rows upserted -> binance_funding_history"
        )
    finally:
        await md.close()
        await hv.close()


if __name__ == "__main__":
    asyncio.run(main())
