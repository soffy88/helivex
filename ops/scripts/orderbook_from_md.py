"""Orderbook adapter — 从统一采集层 iris 的 md.orderbook_features 回填 helivex 的
market_data.orderbook_features(采集层合并 P3, 承接 OHLCV P2a / funding 的同一模式)。

背景: helivex 原自建 NautilusTrader L2 recorder(paper/orderbook_recorder.py, 容器
helivex-l2recorder)订阅 OKX **DEMO**(模拟盘)算微观特征。iris/md 的 orderbook-okx
collector 已中立生产 OKX **LIVE** 同公式特征到 md.orderbook_features。切到 LIVE 更适合
信号(该数据类原为 forward-investment 尚无 backtest)。本 adapter 纯 DB→DB(md 与 helivex
同在 platform-postgres localhost:5434), **无 egress / 不需 OKX creds / 不需 Clash 代理**。

**上线前须停 helivex-l2recorder**(否则 DEMO 与 LIVE 混写同表)。

口径映射(md.orderbook_features → helivex.market_data.orderbook_features):
  - instrument = md.inst_id + '.OKX'(NautilusTrader InstrumentId 格式, e.g. BTC-USDT-SWAP.OKX)
  - book_ts_ns = md.ts 的 epoch 纳秒;其余 14 特征列同名直传
  - size = OKX 原生张数(contracts); imbalance/microprice/spread 单位无关
幂等: helivex 表仅 id serial 无 (instrument,ts) 唯一键 → 用 watermark(每 instrument 只插
已存 max(ts) 之后的行), 避免重复且不改 schema。

回滚: 重启 helivex-l2recorder、disable 本 adapter timer(旧节点保留)。
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

_FEATURE_COLS = (
    "best_bid",
    "best_ask",
    "mid",
    "microprice",
    "spread",
    "spread_bps",
    "bid_sz1",
    "ask_sz1",
    "bid_depth5",
    "ask_depth5",
    "imbalance1",
    "imbalance5",
)


_BATCH_SIZE = 50_000


async def sync(
    md: asyncpg.Pool, hv: asyncpg.Pool, batch_size: int = _BATCH_SIZE
) -> int:
    # watermark: 每 instrument helivex 已存的 max(ts)
    wm_rows = await hv.fetch(
        "SELECT instrument, MAX(ts) AS max_ts FROM market_data.orderbook_features GROUP BY instrument"
    )
    watermark = {r["instrument"]: r["max_ts"] for r in wm_rows}
    # md 表按 10s/inst 持续累积(千万行级), 之前无 ts 下限 + 无 LIMIT 导致每 30s 全表 fetch
    # 进内存 OOM。下推最早 watermark 到 SQL, 配合 LIMIT 分批追, per-instrument 的精确去重
    # 仍靠下面的 Python watermark 比对(不同 instrument 的 watermark 可能不齐)。
    global_wm = min(watermark.values()) if watermark else None

    # md.symbol(BTC-USDT) + inst_id(BTC-USDT-SWAP) -> helivex.instrument(BTC-USDT-SWAP.OKX)
    if global_wm is None:
        md_rows = await md.fetch(
            """
            SELECT symbol, inst_id, ts, best_bid, best_ask, mid, microprice, spread, spread_bps,
                   bid_sz1, ask_sz1, bid_depth5, ask_depth5, imbalance1, imbalance5
            FROM md.orderbook_features
            WHERE venue='okx' AND inst_id IS NOT NULL
            ORDER BY ts ASC
            LIMIT $1
            """,
            batch_size,
        )
    else:
        md_rows = await md.fetch(
            """
            SELECT symbol, inst_id, ts, best_bid, best_ask, mid, microprice, spread, spread_bps,
                   bid_sz1, ask_sz1, bid_depth5, ask_depth5, imbalance1, imbalance5
            FROM md.orderbook_features
            WHERE venue='okx' AND inst_id IS NOT NULL AND ts > $1
            ORDER BY ts ASC
            LIMIT $2
            """,
            global_wm,
            batch_size,
        )

    n = 0
    for r in md_rows:
        instrument = f"{r['inst_id']}.OKX"
        wm = watermark.get(instrument)
        if wm is not None and r["ts"] <= wm:
            continue
        book_ts_ns = int(r["ts"].timestamp() * 1e9)
        await hv.execute(
            """
            INSERT INTO market_data.orderbook_features
                (ts, book_ts_ns, instrument, best_bid, best_ask, mid, microprice,
                 spread, spread_bps, bid_sz1, ask_sz1, bid_depth5, ask_depth5,
                 imbalance1, imbalance5)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15)
            """,
            r["ts"],
            book_ts_ns,
            instrument,
            *(float(r[c]) if r[c] is not None else None for c in _FEATURE_COLS),
        )
        n += 1
    return n


async def main() -> None:
    md = await asyncpg.create_pool(MD_DSN, min_size=1, max_size=2)
    hv = await asyncpg.create_pool(HV_DSN, min_size=1, max_size=2)
    try:
        n = await sync(md, hv)
        print(f"orderbook_from_md: {n} rows inserted -> market_data.orderbook_features")
    finally:
        await md.close()
        await hv.close()


if __name__ == "__main__":
    asyncio.run(main())
