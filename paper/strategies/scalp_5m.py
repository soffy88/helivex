"""Strategy 4: 5M VWAP Mean Reversion scalper (BTC/ETH/SOL USDT-SWAP, long+short).

⚠ GATE STATUS: NO-GO — R5 confirmed cost-killed.
  Gross Sharpe: +1.33 (looks viable without costs)
  Taker cost:   307%/yr at typical OKX rates
  Net Sharpe:   deeply negative OOS
  Deployed?     NO. This is an observation-only paper run.

WHY WE STILL RUN IT:
  Backtest assumed 97% maker fill rate + 2bps cost. Real execution may differ.
  Paper run measures: actual fill type, actual slippage, actual trade frequency.
  If real fill rate >> 97% maker AND cost << 2bps → re-evaluate (unlikely).
  More likely: confirms R5. Gives us real data vs backtest assumptions.

Logic (same as R5 gross signal):
  VWAP z-score on 1H rolling window (12 × 5m bars).
  Enter long:  z < -z_thr  (price below VWAP by 2 sigma)
  Enter short: z > +z_thr  (price above VWAP by 2 sigma)
  Exit:        time-based after hold bars (default 6 = 30min)

Backtest ref: R5.4 — 5M SWAP VWAP-MR, vwap_n=12, z_thr=2.0, hold=6.
Paper purpose: measure real execution (fill rate/slippage) vs backtest assumptions.
"""
from __future__ import annotations

from collections import deque
from typing import Any

from nautilus_trader.config import StrategyConfig
from nautilus_trader.model.data import Bar, BarType
from nautilus_trader.model.enums import LiquiditySide, OrderSide, TimeInForce
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.trading.strategy import Strategy

from paper.audit import sign_signal
from paper.risk import RISK
from paper.db import DB_DSN, DDL, log_signal, log_fill
from paper.db_pool import ResilientPool
from paper.order_ids import next_client_order_id


class Scalp5MConfig(StrategyConfig, frozen=True):
    instrument_id: str
    bar_type: str
    vwap_n:   int   = 12     # 1H rolling window on 5m bars (12 × 5m = 60min)
    z_thr:    float = 2.0    # z-score threshold (same as R5)
    hold:     int   = 6      # bars before time-exit (6 × 5m = 30min)
    qty_usd:  float = 50.0   # small notional — known loser, control paper burn


class Scalp5M(Strategy):
    """5M VWAP-MR scalper. NO-GO gate — observation only, not deployable.

    Runs paper to measure real fill rate / slippage vs R5 backtest assumptions.
    """

    STRATEGY_BASE = "scalp_5m"

    def __init__(self, config: Scalp5MConfig) -> None:
        super().__init__(config)
        self._closes:  deque[float] = deque(maxlen=config.vwap_n + 2)
        self._volumes: deque[float] = deque(maxlen=config.vwap_n + 2)
        self._position:   int   = 0   # 0=flat, +1=long, -1=short
        self._bars_left:  int   = 0
        self._signal_price: float | None = None
        self._pending_signal_id: int | None = None
        self._order_submit_ns: int | None = None
        self._db: ResilientPool | None = None

    def _strategy_id(self) -> str:
        inst = self.config.instrument_id.replace(".", "_").replace("-", "_").lower()
        return f"{self.STRATEGY_BASE}_{inst}"

    def on_start(self) -> None:
        import asyncio
        self._bar_type = BarType.from_str(self.config.bar_type)
        self.subscribe_bars(self._bar_type)
        self.log.info(
            f"[{self._strategy_id()}] started (NO-GO observation) — "
            f"subscribing to {self._bar_type}"
        )
        self._db = ResilientPool(DB_DSN, DDL, name=self._strategy_id(), logger=self.log)
        asyncio.ensure_future(self._db.ensure())
        asyncio.ensure_future(self._rehydrate_position())

    async def _rehydrate_position(self) -> None:
        # After a restart the venue may hold a position this strategy opened before
        # the crash; NT reconciles it into the cache at startup. Recover direction
        # so logic state matches reality. Deferred (reconciliation settles) and
        # guarded (only when still flat) so it never overrides a live signal.
        import asyncio
        await asyncio.sleep(10)
        if self._position != 0:
            return
        try:
            net = float(self.portfolio.net_position(InstrumentId.from_str(self.config.instrument_id)))
        except Exception as exc:
            self.log.warning(f"[{self._strategy_id()}] position rehydrate skipped: {exc}")
            return
        self._position = 1 if net > 0 else (-1 if net < 0 else 0)
        if self._position != 0:
            self.log.info(f"[{self._strategy_id()}] rehydrated _position={self._position} from venue net={net}")

    def on_bar(self, bar: Bar) -> None:
        close  = float(bar.close)
        volume = float(bar.volume)
        self._closes.append(close)
        self._volumes.append(volume)
        self.log.info(f"[on_bar] {bar.bar_type} close={close:.4f} n={len(self._closes)}")

        # Time-based exit (checked first, position can still have bars left)
        if self._position != 0:
            self._bars_left -= 1
            if self._bars_left <= 0:
                self._fire_signal(bar, "time_exit", close, {
                    "n_bars": len(self._closes), "position": self._position, "bars_left": 0,
                })
                return

        if self._position != 0:
            return

        if len(self._closes) < self.config.vwap_n + 1:
            self._fire_signal(bar, "NEUTRAL", close, {
                "n_bars": len(self._closes), "warmup": True,
            })
            return

        c_arr = list(self._closes)
        v_arr = list(self._volumes)

        # VWAP from prior vwap_n bars (shift(1) — mirrors R5 backtest exact logic)
        prior_c = c_arr[-(self.config.vwap_n + 1):-1]
        prior_v = v_arr[-(self.config.vwap_n + 1):-1]
        roll_vc = sum(c * v for c, v in zip(prior_c, prior_v))
        roll_v  = sum(prior_v)
        vwap    = roll_vc / (roll_v + 1e-10)

        import statistics
        if len(prior_c) < 2:
            self._fire_signal(bar, "NEUTRAL", close, {"n_bars": len(self._closes), "warmup": True})
            return
        std = statistics.stdev(prior_c)
        z   = (close - vwap) / (std + 1e-10)

        indic = {
            "vwap":      round(vwap, 4),
            "std":       round(std, 6),
            "z":         round(z, 4),
            "n_bars":    len(self._closes),
            "position":  self._position,
            "bars_left": self._bars_left,
        }

        if z > self.config.z_thr:
            self._fire_signal(bar, "enter_short", close, indic)
        elif z < -self.config.z_thr:
            self._fire_signal(bar, "enter_long", close, indic)
        else:
            self._fire_signal(bar, "NEUTRAL", close, indic)

    def _fire_signal(self, bar: Bar, action: str, price: float, indicators: dict | None = None) -> None:
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
            "gate":     "NO-GO",       # explicit: R5 cost-killed, observation only
        }
        rec = sign_signal(audit_body)

        if self._db is not None:
            _indicators = indicators
            async def _store():
                try:
                    sid = await self._db.execute(lambda conn: log_signal(
                        conn, strat, inst, action, price,
                        audit_record_id=rec["record_id"],
                        fingerprint_hex=rec["fingerprint_hex"],
                        sig_b64=rec.get("sig_b64", ""),
                        indicators=_indicators,
                    ))
                    self._pending_signal_id = sid
                except Exception as exc:
                    self.log.error(f"[{strat}] SIGNAL PERSIST FAILED ({action}): {exc}")
            asyncio.ensure_future(_store())

        self._signal_price = price
        self.log.info(
            f"[{strat}] SIGNAL {action} @ {price:.4f}  "
            f"record={rec['record_id']}  tier={rec['tier']}  [NO-GO obs]"
        )

        if action == "NEUTRAL":
            return

        # ── portfolio risk gate (pre-trade) — see paper/risk.py ──
        if action.startswith("enter"):
            _dec = RISK.gate_entry(strat, inst, self.config.qty_usd)
            if not _dec.allowed:
                self.log.warning(f"[{strat}] ENTRY BLOCKED by risk: {_dec.reason}")
                return
            RISK.open_position(strat, inst, self.config.qty_usd)
        else:
            RISK.close_position(strat, inst)

        instrument = self.cache.instrument(InstrumentId.from_str(self.config.instrument_id))
        if instrument is None:
            self.log.error(f"[{strat}] instrument not found in cache")
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
            client_order_id=next_client_order_id(strat),   # OKX-safe alphanumeric clOrdId
        )
        self._order_submit_ns = self.clock.timestamp_ns()
        self.submit_order(order)
        self.log.info(f"[{strat}] ORDER submitted: {side} {qty}  [NO-GO obs]")

    def on_order_filled(self, event: Any) -> None:
        import asyncio
        if self._db is not None and self._signal_price is not None:
            fill_price = float(str(event.last_px))
            side       = "BUY" if event.order_side == OrderSide.BUY else "SELL"
            qty        = float(str(event.last_qty))
            strat      = self._strategy_id()
            inst       = self.config.instrument_id
            # Reconcile risk exposure to the ACTUAL executed notional (price×qty),
            # replacing the nominal qty_usd estimate from _fire_signal. _position is
            # already updated by _fire_signal: non-zero = entry fill, 0 = exit fill.
            if self._position != 0:
                RISK.open_position(strat, inst, fill_price * qty)
            else:
                RISK.close_position(strat, inst)
            # Real maker/taker from the venue fill report (was hardcoded taker)
            fill_type  = "maker" if getattr(event, "liquidity_side", None) == LiquiditySide.MAKER else "taker"
            # Submit → fill latency (NT clock, ms)
            latency_ms = None
            if self._order_submit_ns is not None:
                latency_ms = max(0, int((self.clock.timestamp_ns() - self._order_submit_ns) / 1_000_000))

            sig_price = self._signal_price
            sig_id = self._pending_signal_id
            async def _store():
                try:
                    await self._db.execute(lambda conn: log_fill(
                        conn, strat, inst, side, qty,
                        signal_price=sig_price,
                        actual_fill_price=fill_price,
                        order_id=str(event.client_order_id),
                        venue_order_id=str(getattr(event, "venue_order_id", "")),
                        latency_ms=latency_ms,
                        fill_type=fill_type,
                        signal_id=sig_id,
                    ))
                except Exception as exc:
                    self.log.error(f"[{strat}] FILL PERSIST FAILED: {exc}")
            asyncio.ensure_future(_store())
            self._pending_signal_id = None
            self._order_submit_ns = None

    def on_stop(self) -> None:
        inst_id = InstrumentId.from_str(self.config.instrument_id)
        self.close_all_positions(inst_id)
        if self._db is not None:
            import asyncio
            asyncio.ensure_future(self._db.close())
            self._db = None
