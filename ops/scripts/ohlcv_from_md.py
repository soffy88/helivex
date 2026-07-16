"""OHLCV adapter — 从统一采集层 iris 的 md.ohlcv 回填 helivex 的 ohlcv 表(采集层合并 P2a + 收尾)。

OKX-swap 4H/5m 不再由 helivex 自采(backfill_4h_extended / backfill_5m),OKX **现货** 1H 也不再自采
(backfill_ohlcv.py)——改读中立生产者 iris 写的 `md.ohlcv`(venue=okx,instrument_type=perp/spot)。
纯 DB→DB(md 与 helivex 同在 platform-postgres),**无 OKX egress、不需 Clash 代理**——比原 ingest
更简单可靠。现货这批是"唯一被确认的跨项目真实重复"(vs helixa 自采 Binance 现货)的 helivex 侧收尾,
见 docs/HELIVEX-IMPL_SPEC-LER-001.md §12 调研 及 iris commit 93ba851。

口径映射(对账已逐项验证一致,见 iris/tools + CONSOLIDATION_PLAN_P2_OHLCV):
  - helivex ts = 收盘时刻 → bar_close_ts = md.bar_open_ts + interval
  - perp: helivex.volume = 张数 → md.volume_contracts;spot: helivex.volume = 基础货币(coin)→ md.volume
    (spot 无张数概念,md.volume_contracts 为 NULL;两口径下 helivex.quote_volume 都 = md.quote_volume,USDT)
  - 只取 md 已收盘 bar(is_final=true),与原 ingest 跳过未确认 bar 一致
  - ON CONFLICT (instrument, bar_close_ts, source) DO NOTHING —— 与原 ingest 相同,幂等、深史不动

回滚:重新 enable helivex-ingest-ohlcv-4h/5m/1h.timer、disable 本 adapter timer 即可(旧脚本保留)。
"""

from __future__ import annotations

import asyncio
import os
from datetime import timedelta

import asyncpg

MD_DSN = os.environ.get(
    "MD_DSN", "postgresql://helios:helios_dev_pass@localhost:5434/marketdata"
)
HV_DSN = os.environ.get(
    "DB_DSN", "postgresql://helios:helios_dev_pass@localhost:5434/helivex"
)

# md symbol -> helivex instrument(OKX swap instId 加 -SWAP;spot 就是归一化形式本身)
_INSTRUMENT_PERP = {
    "BTC-USDT": "BTC-USDT-SWAP",
    "ETH-USDT": "ETH-USDT-SWAP",
    "SOL-USDT": "SOL-USDT-SWAP",
}
_INSTRUMENT_SPOT = {
    "BTC-USDT": "BTC-USDT",
    "ETH-USDT": "ETH-USDT",
    "SOL-USDT": "SOL-USDT",
    # BNB 是 helixa 的标的,不是 helivex 的 INSTRUMENTS 列表(backfill_ohlcv.py),不同步。
}

# (instrument_type, md timeframe, helivex 表, helivex source, interval 秒, symbol map)
_SERIES = [
    ("perp", "h4", "ohlcv_1h", "okx_swap", 4 * 3600, _INSTRUMENT_PERP),
    ("perp", "m5", "ohlcv_5m", "okx_swap_5m", 300, _INSTRUMENT_PERP),
    ("spot", "h1", "ohlcv_1h", "okx", 3600, _INSTRUMENT_SPOT),
]


async def sync_series(
    md: asyncpg.Pool,
    hv: asyncpg.Pool,
    instrument_type: str,
    tf: str,
    table: str,
    source: str,
    interval: int,
    symbol_map: dict[str, str],
) -> int:
    rows = await md.fetch(
        """
        SELECT symbol, bar_open_ts, open, high, low, close,
               volume, volume_contracts, quote_volume
        FROM md.ohlcv
        WHERE venue='okx' AND instrument_type=$1 AND timeframe=$2 AND is_final=true
        """,
        instrument_type,
        tf,
    )
    n = 0
    for r in rows:
        instrument = symbol_map.get(r["symbol"])
        if instrument is None:
            continue
        bar_close_ts = r["bar_open_ts"] + timedelta(seconds=interval)
        # perp: helivex.volume = 张数(volume_contracts);spot: helivex.volume = coin(volume)
        volume = r["volume_contracts"] if instrument_type == "perp" else r["volume"]
        await hv.execute(
            f"""
            INSERT INTO market_data.{table}
                (instrument, bar_close_ts, source, open, high, low, close, volume, quote_volume)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)
            ON CONFLICT (instrument, bar_close_ts, source) DO NOTHING
            """,
            instrument,
            bar_close_ts,
            source,
            r["open"],
            r["high"],
            r["low"],
            r["close"],
            volume,
            r["quote_volume"],  # helivex.quote_volume = USDT,两口径通用
        )
        n += 1
    return n


async def main() -> None:
    md = await asyncpg.create_pool(MD_DSN, min_size=1, max_size=2)
    hv = await asyncpg.create_pool(HV_DSN, min_size=1, max_size=2)
    try:
        for instrument_type, tf, table, source, interval, symbol_map in _SERIES:
            n = await sync_series(
                md, hv, instrument_type, tf, table, source, interval, symbol_map
            )
            print(
                f"ohlcv_from_md: {source} {instrument_type}/{tf} -> {table}: {n} final bars upserted"
            )
    finally:
        await md.close()
        await hv.close()


if __name__ == "__main__":
    asyncio.run(main())
