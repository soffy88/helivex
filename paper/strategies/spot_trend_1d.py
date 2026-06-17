"""Strategy 3: Daily Donchian Spot Trend (long-only, 200d MA bear filter).

Backtest reference: R6.0 — gross Sharpe ~1.1–1.5 but gate FAIL (PBO=1.0, structural long-only bias).
Paper purpose: validate execution assumptions (daily bar close → market fill slippage).
Parameters: N_ENTER=20d breakout, N_EXIT=10d pullback, BEAR_MA=200d filter.
Execution: taker market orders (spot, not SWAP). OKX Demo spot.
"""
from __future__ import annotations

from collections import deque
from typing import Any

import asyncpg

from nautilus_trader.config import StrategyConfig
from nautilus_trader.model.data import Bar, BarType
from nautilus_trader.model.enums import OrderSide, TimeInForce
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.trading.strategy import Strategy

from paper.audit import sign_signal
from paper.db import DB_DSN, ensure_schema, log_signal, log_fill


class SpotTrend1DConfig(StrategyConfig, frozen=True):
    instrument_id: str
    bar_type: str
    n_enter:   int   = 20    # Donchian channel for entry
    n_exit:    int   = 10    # Donchian channel for exit
    bear_ma:   int   = 200   # MA period for bear filter
    qty_usd:   float = 200.0


class SpotTrend1D(Strategy):
    """Daily Donchian trend, long-only spot, with 200d MA bear filter."""

    STRATEGY_BASE = "spot_trend_1d"

    def __init__(self, config: SpotTrend1DConfig) -> None:
        super().__init__(config)
        maxlen = max(config.n_enter, config.n_exit, config.bear_ma) + 2
        self._closes: deque[float] = deque(maxlen=maxlen)
        self._position: int = 0   # 0=flat, 1=long
        self._signal_price: float | None = None
        self._pending_signal_id: int | None = None
        self._db_pool: asyncpg.Pool | None = None

    def _strategy_id(self) -> str:
        inst = self.config.instrument_id.replace(".", "_").replace("-", "_").lower()
        return f"{self.STRATEGY_BASE}_{inst}"

    async def _init_db(self) -> None:
        self._db_pool = await asyncpg.create_pool(DB_DSN, min_size=1, max_size=2)
        async with self._db_pool.acquire() as conn:
            await ensure_schema(conn)

    def on_start(self) -> None:
        import asyncio
        self._bar_type = BarType.from_str(self.config.bar_type)
        self.subscribe_bars(self._bar_type)
        asyncio.ensure_future(self._init_db())

    def on_bar(self, bar: Bar) -> None:
        close = float(bar.close)
        self._closes.append(close)
        self.log.info(f"[on_bar] {bar.bar_type} close={close:.4f} n={len(self._closes)}")

        c = self.config
        need = max(c.n_enter, c.n_exit, c.bear_ma) + 1
        if len(self._closes) < need:
            self._fire_signal(bar, "NEUTRAL", close, {"n_bars": len(self._closes), "warmup": True})
            return

        closes_list = list(self._closes)

        # Bear filter: skip new entries when close < 200d SMA
        ma200 = sum(closes_list[-(c.bear_ma + 1):-1]) / c.bear_ma
        bear = close < ma200

        # Donchian channels from prior bars (shift=1, no look-ahead)
        high_enter = max(closes_list[-(c.n_enter + 1):-1])
        low_exit   = min(closes_list[-(c.n_exit + 1):-1])

        action: str | None = None

        if self._position == 0:
            if not bear and close > high_enter:
                action = "enter_long"
        elif self._position == 1:
            if close < low_exit:
                action = "exit_long"

        indic = {
            "ma200":      round(ma200, 4),
            "bear":       bear,
            "high_enter": round(high_enter, 4),
            "low_exit":   round(low_exit, 4),
            "n_bars":     len(self._closes),
            "position":   self._position,
        }
        self._fire_signal(bar, action or "NEUTRAL", close, indic)

    def _fire_signal(self, bar: Bar, action: str, price: float, indicators: dict | None = None) -> None:
        import asyncio
        strat = self._strategy_id()
        inst  = self.config.instrument_id

        audit_body = {
            "strategy": strat,
            "action":   action,
            "price":    price,
            "bar_ts":   bar.ts_event,
            "n_enter":  self.config.n_enter,
            "n_exit":   self.config.n_exit,
            "bear_ma":  self.config.bear_ma,
        }
        rec = sign_signal(audit_body)

        if self._db_pool:
            _indicators = indicators
            async def _store():
                async with self._db_pool.acquire() as conn:
                    sid = await log_signal(
                        conn, strat, inst, action, price,
                        audit_record_id=rec["record_id"],
                        fingerprint_hex=rec["fingerprint_hex"],
                        sig_b64=rec.get("sig_b64", ""),
                        indicators=_indicators,
                    )
                    self._pending_signal_id = sid
            asyncio.ensure_future(_store())

        self._signal_price = price
        self.log.info(
            f"[{strat}] SIGNAL {action} @ {price:.4f}  "
            f"record={rec['record_id']}  tier={rec['tier']}"
        )

        if action == "NEUTRAL":
            return

        instrument = self.cache.instrument(InstrumentId.from_str(self.config.instrument_id))
        if instrument is None:
            return

        qty = instrument.min_quantity

        if action == "enter_long":
            side = OrderSide.BUY; self._position = 1
        elif action == "exit_long":
            side = OrderSide.SELL; self._position = 0
        else:
            return

        order = self.order_factory.market(
            instrument_id=instrument.id,
            order_side=side,
            quantity=qty,
            time_in_force=TimeInForce.IOC,
        )
        self.submit_order(order)

    def on_order_filled(self, event: Any) -> None:
        import asyncio
        if self._db_pool and self._signal_price is not None:
            fill_price = float(str(event.last_px))
            side       = "BUY" if event.order_side == OrderSide.BUY else "SELL"
            qty        = float(str(event.last_qty))
            strat      = self._strategy_id()
            inst       = self.config.instrument_id

            async def _store():
                async with self._db_pool.acquire() as conn:
                    await log_fill(
                        conn, strat, inst, side, qty,
                        signal_price=self._signal_price,
                        actual_fill_price=fill_price,
                        order_id=str(event.client_order_id),
                        fill_type="taker",
                        signal_id=self._pending_signal_id,
                    )
            asyncio.ensure_future(_store())
            self._pending_signal_id = None

    def on_stop(self) -> None:
        inst_id = InstrumentId.from_str(self.config.instrument_id)
        self.close_all_positions(inst_id)
        if self._db_pool:
            import asyncio
            asyncio.ensure_future(self._db_pool.close())
            self._db_pool = None
