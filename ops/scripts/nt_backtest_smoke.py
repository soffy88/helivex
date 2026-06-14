"""Minimal NautilusTrader BacktestEngine smoke test — no external data needed."""
from decimal import Decimal

import pandas as pd

from nautilus_trader.backtest.engine import BacktestEngine
from nautilus_trader.backtest.models import FillModel
from nautilus_trader.config import BacktestEngineConfig
from nautilus_trader.model.currencies import USD, BTC
from nautilus_trader.model.data import Bar, BarType
from nautilus_trader.model.enums import AccountType, OmsType, OrderSide, BarAggregation, PriceType
from nautilus_trader.model.identifiers import Venue, TraderId
from nautilus_trader.model.instruments import CurrencyPair
from nautilus_trader.model.objects import Money, Price, Quantity
from nautilus_trader.test_kit.providers import TestInstrumentProvider
from nautilus_trader.trading.strategy import Strategy
from nautilus_trader.config import StrategyConfig


class SmokeConfig(StrategyConfig, frozen=True):
    instrument_id: str
    bar_type: str


class SmokeStrategy(Strategy):
    """Buy on first bar, sell on last — just to produce a trade and P&L."""

    def __init__(self, config: SmokeConfig):
        super().__init__(config)
        self.bought = False

    def on_start(self):
        from nautilus_trader.model.identifiers import InstrumentId
        from nautilus_trader.model.data import BarType
        self._bar_type = BarType.from_str(self.config.bar_type)
        self._instrument_id = InstrumentId.from_str(self.config.instrument_id)
        self.subscribe_bars(self._bar_type)

    def on_bar(self, bar: Bar):
        if not self.bought:
            self.buy(bar)
            self.bought = True

    def buy(self, bar: Bar):
        instrument = self.cache.instrument(self._instrument_id)
        order = self.order_factory.market(
            instrument_id=self._instrument_id,
            order_side=OrderSide.BUY,
            quantity=instrument.make_qty(0.01),
        )
        self.submit_order(order)

    def on_stop(self):
        self.close_all_positions(self._instrument_id)


def make_bars(instrument, n=50):
    bar_type = BarType.from_str(
        f"{instrument.id}-1-MINUTE-LAST-EXTERNAL"
    )
    bars = []
    base = pd.Timestamp("2024-01-01", tz="UTC")
    price = 40_000.0
    for i in range(n):
        ts = int((base + pd.Timedelta(minutes=i)).timestamp() * 1e9)
        bars.append(
            Bar(
                bar_type=bar_type,
                open=Price.from_str(f"{price:.2f}"),
                high=Price.from_str(f"{price + 50:.2f}"),
                low=Price.from_str(f"{price - 50:.2f}"),
                close=Price.from_str(f"{price + (i % 10 - 5) * 10:.2f}"),
                volume=Quantity.from_str("1.000000"),
                ts_event=ts,
                ts_init=ts,
            )
        )
        price += 20
    return bar_type, bars


def main():
    engine = BacktestEngine(config=BacktestEngineConfig(trader_id=TraderId("TESTER-001")))

    instrument = TestInstrumentProvider.btcusdt_binance()
    venue = instrument.venue  # BINANCE

    engine.add_venue(
        venue=venue,
        oms_type=OmsType.NETTING,
        account_type=AccountType.MARGIN,
        starting_balances=[Money(100_000, USD)],
        fill_model=FillModel(),
        base_currency=USD,
    )

    engine.add_instrument(instrument)

    bar_type, bars = make_bars(instrument)
    engine.add_data(bars)

    strategy = SmokeStrategy(
        config=SmokeConfig(
            instrument_id=str(instrument.id),
            bar_type=str(bar_type),
        )
    )
    engine.add_strategy(strategy)

    engine.run()

    stats = engine.trader.generate_account_report(venue)
    print("=== BacktestEngine smoke test PASSED ===")
    print(f"Bars processed: {len(bars)}")
    print(stats.to_string())

    engine.dispose()


if __name__ == "__main__":
    main()
