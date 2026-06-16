"""Strategy 2: VWAP Mean Reversion 1H (SOL + BTC USDT-SWAP, taker execution).

Backtest reference: R5.3 — SOL 1H gross=+1.155, @10bps=+0.074.
Paper purpose: validate fill slippage vs backtest (signal_price=bar.close, actual=fill).
Parameters: vwap_n=4 (4H window), z_thr=2.0, hold=6 bars (6H).
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


class VwapMR1HConfig(StrategyConfig, frozen=True):
    instrument_id: str
    bar_type: str
    vwap_n: int   = 4      # 4H VWAP window
    z_thr:  float = 2.0
    hold:   int   = 6      # bars before time-exit
    qty_usd: float = 200.0


class VwapMR1H(Strategy):
    """VWAP mean reversion 1H via taker market orders."""

    STRATEGY_BASE = "vwap_mr_1h"

    def __init__(self, config: VwapMR1HConfig) -> None:
        super().__init__(config)
        self._closes:  deque[float] = deque(maxlen=config.vwap_n + 2)
        self._volumes: deque[float] = deque(maxlen=config.vwap_n + 2)
        self._position:   int   = 0   # 0=flat, +1=long, -1=short
        self._bars_left:  int   = 0
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
        close  = float(bar.close)
        volume = float(bar.volume)
        self._closes.append(close)
        self._volumes.append(volume)
        self.log.info(f"[on_bar] {bar.bar_type} close={close:.4f} n={len(self._closes)}")

        # Decrement hold / time-based exit
        if self._position != 0:
            self._bars_left -= 1
            if self._bars_left <= 0:
                self._fire_signal(bar, "time_exit", close)
                return

        if self._position != 0:
            return

        # Need at least vwap_n+1 bars
        if len(self._closes) < self.config.vwap_n + 1:
            self._fire_signal(bar, "NEUTRAL", close)
            return

        c_arr = list(self._closes)
        v_arr = list(self._volumes)

        # VWAP from prior vwap_n bars (shift(1) equivalent)
        prior_c = c_arr[-(self.config.vwap_n + 1):-1]
        prior_v = v_arr[-(self.config.vwap_n + 1):-1]
        roll_vc  = sum(c * v for c, v in zip(prior_c, prior_v))
        roll_v   = sum(prior_v)
        vwap     = roll_vc / (roll_v + 1e-10)

        import statistics
        if len(prior_c) < 2:
            self._fire_signal(bar, "NEUTRAL", close)
            return
        std = statistics.stdev(prior_c)
        z   = (close - vwap) / (std + 1e-10)

        if z > self.config.z_thr:
            self._fire_signal(bar, "enter_short", close)
        elif z < -self.config.z_thr:
            self._fire_signal(bar, "enter_long", close)
        else:
            self._fire_signal(bar, "NEUTRAL", close)

    def _fire_signal(self, bar: Bar, action: str, price: float) -> None:
        import asyncio
        strat = self._strategy_id()
        inst  = self.config.instrument_id

        audit_body = {
            "strategy": strat,
            "action":   action,
            "price":    price,
            "bar_ts":   bar.ts_event,
            "vwap_n":   self.config.vwap_n,
            "z_thr":    self.config.z_thr,
        }
        rec = sign_signal(audit_body)

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

        if action == "enter_short":
            side = OrderSide.SELL; self._position = -1; self._bars_left = self.config.hold
        elif action == "enter_long":
            side = OrderSide.BUY;  self._position = 1;  self._bars_left = self.config.hold
        elif action == "time_exit":
            side = OrderSide.BUY if self._position == -1 else OrderSide.SELL
            self._position = 0; self._bars_left = 0
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
