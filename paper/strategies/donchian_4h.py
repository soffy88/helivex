"""Strategy 1: 4H Donchian Channel trend following (SWAP, long + short).

Backtest reference: R4.4 (Donchian N=20 on 4H bars, BTC/ETH/SOL USDT-SWAP).
Paper purpose: validate fill execution assumptions (slippage at bar close).

Signal (same logic as R4.4 backtest, no HMM filter for live simplicity):
  ENTER LONG:  close breaks above rolling 20-bar high (breakout)
  ENTER SHORT: close breaks below rolling 20-bar low
  EXIT LONG:   close falls below rolling 10-bar low
  EXIT SHORT:  close rises above rolling 10-bar high

One position per instrument at a time. Fixed quantity (small notional).
"""
from __future__ import annotations

import datetime
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


class Donchian4HConfig(StrategyConfig, frozen=True):
    instrument_id: str
    bar_type: str
    n_enter: int = 20   # Donchian window for entry
    n_exit:  int = 10   # Donchian window for exit
    qty_usd: float = 200.0  # notional per trade in USD


class Donchian4H(Strategy):
    """4H Donchian trend strategy wired to OKX Demo execution."""

    STRATEGY_BASE = "donchian_4h"

    def __init__(self, config: Donchian4HConfig) -> None:
        super().__init__(config)
        n = max(config.n_enter, config.n_exit)
        self._closes: deque[float] = deque(maxlen=n + 2)
        self._position: int = 0       # 0=flat, +1=long, -1=short
        self._signal_price: float | None = None
        self._signal_ts: int | None = None
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
        self._bar_type     = BarType.from_str(self.config.bar_type)
        self._instrument_id_obj = InstrumentId.from_str(self.config.instrument_id)
        self.subscribe_bars(self._bar_type)
        self.log.info(f"[{self._strategy_id()}] started, subscribing to {self._bar_type}")
        # Schedule DB init as a task on NT's running loop; pool ready before first bar.
        asyncio.ensure_future(self._init_db())

    def on_bar(self, bar: Bar) -> None:
        close = float(bar.close)
        self._closes.append(close)
        self.log.info(f"[on_bar] {bar.bar_type} close={close:.4f} n={len(self._closes)}")

        c = self.config
        if len(self._closes) < c.n_enter + 1:
            self._fire_signal(bar, "NEUTRAL", close)
            return

        closes_list = list(self._closes)

        high_enter = max(closes_list[-(c.n_enter + 1):-1])   # prior n_enter bars
        low_enter  = min(closes_list[-(c.n_enter + 1):-1])
        high_exit  = max(closes_list[-(c.n_exit + 1):-1])
        low_exit   = min(closes_list[-(c.n_exit + 1):-1])

        action: str | None = None

        if self._position == 0:
            if close > high_enter:
                action = "enter_long"
            elif close < low_enter:
                action = "enter_short"
        elif self._position == 1:
            if close < low_exit:
                action = "exit_long"
        elif self._position == -1:
            if close > high_exit:
                action = "exit_short"

        self._fire_signal(bar, action or "NEUTRAL", close)

    def _fire_signal(self, bar: Bar, action: str, price: float) -> None:
        import asyncio

        inst = self.config.instrument_id
        strat = self._strategy_id()

        audit_body = {
            "strategy": strat,
            "action":   action,
            "price":    price,
            "bar_ts":   bar.ts_event,
            "n_enter":  self.config.n_enter,
            "n_exit":   self.config.n_exit,
        }
        rec = sign_signal(audit_body)

        # Persist signal
        if self._db_pool:
            async def _store():
                async with self._db_pool.acquire() as conn:
                    sid = await log_signal(
                        conn, strat, inst, action, price,
                        audit_record_id=rec["record_id"],
                        fingerprint_hex=rec["fingerprint_hex"],
                        sig_b64=rec.get("sig_b64", ""),
                    )
                    self._pending_signal_id = sid
            asyncio.ensure_future(_store())

        self._signal_price = price
        self._signal_ts    = bar.ts_event
        self.log.info(
            f"[{strat}] SIGNAL {action} @ {price:.2f}  "
            f"record={rec['record_id']}  tier={rec['tier']}"
        )

        if action == "NEUTRAL":
            return

        # Submit order
        instrument = self.cache.instrument(
            InstrumentId.from_str(self.config.instrument_id)
        )
        if instrument is None:
            self.log.error(f"[{strat}] instrument not found in cache")
            return

        qty = instrument.make_qty(self.config.qty_usd / float(instrument.settlement_price or 1))
        if qty is None or float(str(qty)) < float(str(instrument.min_quantity)):
            qty = instrument.min_quantity

        if action == "enter_long":
            side = OrderSide.BUY; self._position = 1
        elif action == "enter_short":
            side = OrderSide.SELL; self._position = -1
        elif action in ("exit_long", "exit_short"):
            side = OrderSide.SELL if self._position == 1 else OrderSide.BUY
            self._position = 0
        else:
            return

        order = self.order_factory.market(
            instrument_id=instrument.id,
            order_side=side,
            quantity=qty,
            time_in_force=TimeInForce.IOC,
        )
        self.submit_order(order)
        self.log.info(f"[{strat}] ORDER submitted: {side} {qty}")

    def on_order_filled(self, event: Any) -> None:
        import asyncio
        if self._db_pool and self._signal_price is not None:
            fill_price = float(str(event.last_px))
            side       = "BUY" if event.order_side == OrderSide.BUY else "SELL"
            qty        = float(str(event.last_qty))
            strat      = self._strategy_id()
            inst       = self.config.instrument_id
            sig_id     = self._pending_signal_id

            async def _store():
                async with self._db_pool.acquire() as conn:
                    await log_fill(
                        conn, strat, inst, side, qty,
                        signal_price=self._signal_price,
                        actual_fill_price=fill_price,
                        order_id=str(event.client_order_id),
                        venue_order_id=str(getattr(event, "venue_order_id", "")),
                        fill_type="taker",
                        signal_id=sig_id,
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
