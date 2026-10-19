"""数据工具：TimescaleDB → OHLCV parquet（研究态只读）。

适配器策略（strategies_port）只需 OHLCV + volume，不需要 run_features 的特征列；
本模块从 DB 拉原生 bar 并按目标周期聚合，写出引擎可读的 parquet。

注意：
- 5m 表（okx_swap_5m）覆盖 2026-04-02 → 至今；1h 表（okx_swap）覆盖 2019-12 → 至今。
- funding 统一 0.0：binance_funding_history 仅覆盖 2026-06-30 起；本批策略为 bar-close
  驱动研究，funding 影响 <1bp/回合量级，报告中注明。
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
from pathlib import Path

import asyncpg
import numpy as np
import pandas as pd

DB_DSN = "postgresql://helios:helios_dev_pass@localhost:5434/helivex"

SRC_BY_TIMEFRAME = {
    "5m": "okx_swap_5m",   # 原生 5m（2026-04-02 起）
    "1h": "okx_swap_5m",   # 真 1H：由 5m 聚合（表 ohlcv_1h/source=okx_swap 实为 4H bars）
    "4h": "okx_swap",      # 原生 4H（表 ohlcv_1h，2019-12 起，~180 根/月）
    "1d": "okx_swap",      # 1D：由 4H 聚合
    "15m": "okx_swap_5m",  # 15min：由 5m 聚合
}
# 原生源 → 目标周期聚合规则（None = 原生直接用）
AGG = {"1h": "1h", "4h": None, "1d": "1D", "15m": "15min"}


async def _fetch_ohlcv(instrument: str, src: str, start: datetime, end: datetime) -> list:
    conn = await asyncpg.connect(DB_DSN)
    try:
        return await conn.fetch(
            """SELECT bar_close_ts,
                      open::float, high::float, low::float, close::float, volume::float
               FROM market_data.ohlcv_1h
               WHERE instrument = $1 AND source = $2
                 AND bar_close_ts >= $3 AND bar_close_ts < $4
               ORDER BY bar_close_ts""",
            instrument, src, start, end,
        )
    finally:
        await conn.close()


async def _fetch_ohlcv_5m(instrument: str, start: datetime, end: datetime) -> list:
    conn = await asyncpg.connect(DB_DSN)
    try:
        return await conn.fetch(
            """SELECT bar_close_ts,
                      open::float, high::float, low::float, close::float, volume::float
               FROM market_data.ohlcv_5m
               WHERE instrument = $1 AND source = $2
                 AND bar_close_ts >= $3 AND bar_close_ts < $4
               ORDER BY bar_close_ts""",
            instrument, "okx_swap_5m", start, end,
        )
    finally:
        await conn.close()


def _resample(df: pd.DataFrame, rule: str) -> pd.DataFrame:
    h = (
        df.set_index("ts")
        .resample(rule, label="right", closed="right")
        .agg(open=("open", "first"), high=("high", "max"),
             low=("low", "min"), close=("close", "last"), volume=("volume", "sum"))
        .dropna()
    )
    return h.reset_index()


async def _fetch_funding_rates(instrument: str, start: datetime, end: datetime) -> list:
    """OKX 8h 结算 funding（market_data.funding_rates，2026-03-16 起）。"""
    conn = await asyncpg.connect(DB_DSN)
    try:
        return await conn.fetch(
            """SELECT ts, funding_rate::float
               FROM market_data.funding_rates
               WHERE instrument = $1 AND ts >= $2 AND ts < $3
               ORDER BY ts""",
            instrument, start, end,
        )
    finally:
        await conn.close()


def _stamp_funding(df: pd.DataFrame, funding_rows: list) -> pd.DataFrame:
    """把 8h 结算 funding stamp 到结算时刻对应的 bar（bar_close_ts 精确对齐）。"""
    if not funding_rows:
        return df
    f = pd.DataFrame(funding_rows, columns=["ts", "funding_rate"])
    f["ts"] = pd.to_datetime(f["ts"], utc=True)
    merged = df.merge(f, how="left", left_on="bar_close_ts", right_on="ts",
                      suffixes=("", "_f"))
    merged["funding_rate"] = merged["funding_rate_f"].fillna(0.0)
    return merged.drop(columns=["ts", "funding_rate_f"])


def build_parquet(
    instrument: str,
    timeframe: str,
    start: str,
    end: str,
    out: str | Path,
    with_funding: bool = True,
) -> int:
    """拉取 + 聚合 + 写 parquet。返回 bar 数。"""
    t0 = datetime.fromisoformat(start).replace(tzinfo=timezone.utc)
    t1 = datetime.fromisoformat(end).replace(tzinfo=timezone.utc)
    if timeframe == "5m":
        rows = asyncio.run(_fetch_ohlcv_5m(instrument, t0, t1))
    elif timeframe in ("1h", "4h", "1d", "15m"):
        if timeframe in ("4h", "1d"):
            rows = asyncio.run(_fetch_ohlcv(instrument, SRC_BY_TIMEFRAME["4h"], t0, t1))
        else:
            rows = asyncio.run(_fetch_ohlcv_5m(instrument, t0, t1))
    else:
        raise ValueError(f"unsupported timeframe {timeframe!r}")

    df = pd.DataFrame(
        rows, columns=["ts", "open", "high", "low", "close", "volume"]
    )
    df["ts"] = pd.to_datetime(df["ts"], utc=True)
    if df.empty:
        raise ValueError(f"no data for {instrument} in [{start}, {end})")
    rule = AGG.get(timeframe)
    if rule is not None:
        df = _resample(df, rule)

    df = df.rename(columns={"ts": "bar_close_ts"})
    df["log_return"] = np.log(df["close"] / df["close"].shift(1)).fillna(0.0)
    df["funding_rate"] = 0.0
    if with_funding and timeframe in ("5m", "1h"):
        fr = asyncio.run(_fetch_funding_rates(instrument, t0, t1))
        df = _stamp_funding(df, fr)
    # 注意：不写 realized_var/tail_index/regime/risk_multiplier 伪列，
    # bridge 据此区分 run_features 产物（含特征列）与纯 OHLCV 产物。

    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(out, index=False)
    return len(df)


def main() -> None:
    p = argparse.ArgumentParser(description="DB → OHLCV parquet（适配器策略用）")
    p.add_argument("--symbol", default="BTC-USDT-SWAP")
    p.add_argument("--timeframe", choices=["5m", "15m", "1h", "4h", "1d"], default="1h")
    p.add_argument("--start", required=True)
    p.add_argument("--end", required=True)
    p.add_argument("--out", required=True)
    args = p.parse_args()
    n = build_parquet(args.symbol, args.timeframe, args.start, args.end, args.out)
    print(f"wrote {n} bars → {args.out}")


if __name__ == "__main__":
    main()
