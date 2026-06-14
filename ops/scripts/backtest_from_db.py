"""NautilusTrader backtest reading bars from market_data.ohlcv_1h.

Data flow: market_data DB → Bar objects → BacktestEngine (deterministic)
Not a live API call — fixed historical data, fully reproducible.

Usage:
    python ops/scripts/backtest_from_db.py [--instrument BTC-USDT] [--months 6]
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import asyncpg

DB_DSN = "postgresql://helios:helios_dev_pass@localhost:5434/helivex"


def load_bars_sync(instrument: str, months: int) -> list[dict]:
    import asyncio

    async def _fetch():
        conn = await asyncpg.connect(DB_DSN)
        cutoff = datetime.now(timezone.utc) - timedelta(days=30 * months)
        rows = await conn.fetch(
            """
            SELECT bar_close_ts, open, high, low, close, volume
            FROM market_data.ohlcv_1h
            WHERE instrument = $1 AND bar_close_ts >= $2
            ORDER BY bar_close_ts ASC
            """,
            instrument,
            cutoff,
        )
        await conn.close()
        return [dict(r) for r in rows]

    return asyncio.run(_fetch())


_run_counter = 0


def run_backtest(instrument: str, months: int) -> dict:
    global _run_counter
    _run_counter += 1
    import pandas as pd
    from nautilus_trader.backtest.engine import BacktestEngine
    from nautilus_trader.backtest.models import FillModel
    from nautilus_trader.config import BacktestEngineConfig
    from nautilus_trader.model.currencies import USD, BTC
    from nautilus_trader.model.data import Bar, BarType
    from nautilus_trader.model.enums import AccountType, OmsType
    from nautilus_trader.model.identifiers import Venue, TraderId, InstrumentId
    from nautilus_trader.model.instruments import CurrencyPair
    from nautilus_trader.model.objects import Money, Price, Quantity, Currency
    from nautilus_trader.test_kit.providers import TestInstrumentProvider

    from services.shell_strategy import ShellConfig, ShellStrategy

    rows = load_bars_sync(instrument, months)
    print(f"Loaded {len(rows)} bars from DB for {instrument} (last {months} months)")
    if not rows:
        raise RuntimeError(f"No bars for {instrument} in DB — run backfill_ohlcv.py first")

    # Use Binance BTCUSDT instrument definition as a proxy (same contract semantics)
    nt_instrument = TestInstrumentProvider.btcusdt_binance()
    venue = nt_instrument.venue  # BINANCE (simulated)

    engine = BacktestEngine(
        config=BacktestEngineConfig(trader_id=TraderId(f"HELIVEX-{_run_counter:03d}"))
    )
    engine.add_venue(
        venue=venue,
        oms_type=OmsType.NETTING,
        account_type=AccountType.MARGIN,
        starting_balances=[Money(100_000, USD)],
        fill_model=FillModel(),
        base_currency=USD,
    )
    engine.add_instrument(nt_instrument)

    bar_type = BarType.from_str(f"{nt_instrument.id}-1-HOUR-LAST-EXTERNAL")

    bars: list[Bar] = []
    for r in rows:
        ts_ns = int(r["bar_close_ts"].timestamp() * 1e9)
        bars.append(Bar(
            bar_type=bar_type,
            open=Price.from_str(f"{float(r['open']):.2f}"),
            high=Price.from_str(f"{float(r['high']):.2f}"),
            low=Price.from_str(f"{float(r['low']):.2f}"),
            close=Price.from_str(f"{float(r['close']):.2f}"),
            volume=Quantity.from_str(f"{float(r['volume']):.6f}"),
            ts_event=ts_ns,
            ts_init=ts_ns,
        ))

    engine.add_data(bars)

    inst_id = str(nt_instrument.id)
    strategy = ShellStrategy(config=ShellConfig(
        instrument_id=inst_id,
        bar_type=str(bar_type),
        capital_usd=100_000.0,
    ))
    engine.add_strategy(strategy)

    engine.run()

    fills = engine.trader.generate_fills_report()
    fill_count = len(fills)

    # Compute realized P&L from fills (long-only NETTING: +BUY, -SELL)
    realized_pnl = 0.0
    position = 0.0
    avg_cost = 0.0
    for _, row in fills.iterrows():
        qty = float(row["last_qty"])
        px = float(row["last_px"])
        comm = float(str(row["commission"]).split()[0]) if row["commission"] else 0.0
        if str(row["order_side"]) == "BUY":
            avg_cost = (avg_cost * position + px * qty) / (position + qty) if position + qty else px
            position += qty
        else:  # SELL
            realized_pnl += (px - avg_cost) * qty
            position -= qty
        realized_pnl -= comm  # subtract commission

    engine.dispose()

    return {
        "instrument": instrument,
        "months": months,
        "bars_loaded": len(rows),
        "fill_count": fill_count,
        "realized_pnl_usd": round(realized_pnl, 4),
        "strategy": "bocpd_trend_following (omodul)",
        "data_source": "market_data.ohlcv_1h (DB, not live API)",
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--instrument", default="BTC-USDT")
    parser.add_argument("--months", type=int, default=6)
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()

    result = run_backtest(args.instrument, args.months)
    print("\n=== Backtest Result ===")
    for k, v in result.items():
        print(f"  {k:20s}: {v}")


if __name__ == "__main__":
    main()
