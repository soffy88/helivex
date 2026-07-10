"""Strategy 3: Daily Donchian Spot Trend (long-only, 200d MA bear filter).

Backtest reference: R6.0 — gross Sharpe ~1.1–1.5 but gate FAIL (PBO=1.0, structural long-only bias).
Paper purpose: validate execution assumptions (daily bar close → market fill slippage).
Parameters: N_ENTER=20d breakout, N_EXIT=10d pullback, BEAR_MA=200d filter.
Execution: taker market orders (spot, not SWAP). OKX Demo spot.
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
from paper.risk import RISK, log_risk_event
from paper.db import DB_DSN, DDL, log_signal, log_fill
from paper.db_pool import ResilientPool
from paper.order_ids import next_client_order_id


class SpotTrend1DConfig(StrategyConfig, frozen=True):
    instrument_id: str
    bar_type: str
    n_enter: int = 20  # Donchian channel for entry
    n_exit: int = 10  # Donchian channel for exit
    bear_ma: int = 200  # MA period for bear filter
    qty_usd: float = 200.0


class SpotTrend1D(Strategy):
    """Daily Donchian trend, long-only spot, with 200d MA bear filter."""

    STRATEGY_BASE = "spot_trend_1d"

    def __init__(self, config: SpotTrend1DConfig) -> None:
        super().__init__(config)
        maxlen = max(config.n_enter, config.n_exit, config.bear_ma) + 2
        self._closes: deque[float] = deque(maxlen=maxlen)
        self._position: int = 0  # 0=flat, 1=long
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
        self._db = ResilientPool(DB_DSN, DDL, name=self._strategy_id(), logger=self.log)
        asyncio.ensure_future(self._boot())
        asyncio.ensure_future(self._rehydrate_position())

    async def _boot(self) -> None:
        import asyncio

        await self._db.ensure()
        await self._seed_warmup()
        # Catch-up: 日线策略一天只有一次决策窗口(00:00 UTC 收盘),重启跨过收盘
        # 就要再等 24h——频繁重启会系统性掐掉它的开仓机会。刚错过(≤30min)则补做
        # 一次真实决策;更旧不追。
        await asyncio.sleep(12)  # 让 _rehydrate_position 先落定
        self._catchup_eval()

    def _catchup_eval(self) -> None:
        import datetime as _dt

        last = getattr(self, "_seed_last_close", None)
        if last is None or not self._closes:
            return
        age = (_dt.datetime.now(_dt.timezone.utc) - last).total_seconds()
        if age > 1800:
            return
        self.log.info(
            f"[{self._strategy_id()}] catch-up eval: missed daily close {age:.0f}s ago during restart"
        )
        self._evaluate(self.clock.timestamp_ns())

    async def _seed_warmup(self) -> None:
        # Seed daily closes from market_data.ohlcv_1h resampled to 1D. Without this
        # the 200d bear-MA + 21-bar Donchian would need ~7 MONTHS of uninterrupted
        # node uptime before the first entry decision. Uses SWAP history as proxy for
        # the spot instrument (this strategy is already documented as a SWAP proxy;
        # DB spot history only goes back ~1 month, SWAP has 2 years). Seeds HISTORY
        # ONLY — no evaluation, no order can fire from stale data; the next live
        # daily close makes the first real decision.
        sid = self._strategy_id()
        if self._closes:  # live bars already arrived — don't splice history under them
            return
        spot = self.config.instrument_id.split(".")[0]  # e.g. BTC-USDT
        inst_db = spot if spot.endswith("-SWAP") else f"{spot}-SWAP"
        limit = self._closes.maxlen or 202
        try:
            rows = await self._db.execute(
                lambda conn: conn.fetch(
                    """SELECT date_trunc('day', bar_close_ts - interval '1 second') AS d,
                          (array_agg(close ORDER BY bar_close_ts DESC))[1] AS c
                     FROM market_data.ohlcv_1h
                     WHERE instrument = $1 AND source = 'okx_swap'
                       AND bar_close_ts <= date_trunc('day', now())
                     GROUP BY 1 ORDER BY d DESC LIMIT $2""",
                    inst_db,
                    limit,
                )
            )
        except Exception as exc:
            self.log.warning(f"[{sid}] warmup seed skipped: {exc}")
            return
        if not rows:
            self.log.warning(f"[{sid}] warmup seed: no daily history for {inst_db}")
            return
        for r in reversed(rows):  # chronological
            self._closes.append(float(r["c"]))
        # date_trunc 返回当日 0 点;桶收盘 = d + 1day。供 _catchup_eval 判断新鲜度。
        import datetime as _dt
        self._seed_last_close = rows[0]["d"] + _dt.timedelta(days=1)
        self.log.info(
            f"[{sid}] warmup seeded {len(rows)} daily closes from ohlcv_1h ({inst_db} proxy)"
        )

    async def _rehydrate_position(self) -> None:
        # After a restart the venue may hold a position this strategy opened before
        # the crash; NT reconciles it into the cache at startup. Recover direction
        # so logic state matches reality. Deferred (reconciliation settles) and
        # guarded (only when still flat) so it never overrides a live signal.
        # Spot is long-only, so only a long (net > 0) is expected.
        import asyncio

        await asyncio.sleep(10)
        if self._position != 0:
            return
        try:
            net = float(
                self.portfolio.net_position(
                    InstrumentId.from_str(self.config.instrument_id)
                )
            )
        except Exception as exc:
            self.log.warning(
                f"[{self._strategy_id()}] position rehydrate skipped: {exc}"
            )
            return
        self._position = 1 if net > 0 else 0
        if self._position != 0:
            self.log.info(
                f"[{self._strategy_id()}] rehydrated _position={self._position} from venue net={net}"
            )

    def on_bar(self, bar: Bar) -> None:
        close = float(bar.close)
        self._closes.append(close)
        self.log.info(
            f"[on_bar] {bar.bar_type} close={close:.4f} n={len(self._closes)}"
        )
        self._evaluate(int(bar.ts_event))

    def _evaluate(self, ts_event: int) -> None:
        close = float(self._closes[-1])
        c = self.config
        need = max(c.n_enter, c.n_exit, c.bear_ma) + 1
        if len(self._closes) < need:
            self._fire_signal(
                ts_event, "NEUTRAL", close, {"n_bars": len(self._closes), "warmup": True}
            )
            return

        closes_list = list(self._closes)

        # Bear filter: skip new entries when close < 200d SMA
        ma200 = sum(closes_list[-(c.bear_ma + 1) : -1]) / c.bear_ma
        bear = close < ma200

        # Donchian channels from prior bars (shift=1, no look-ahead)
        high_enter = max(closes_list[-(c.n_enter + 1) : -1])
        low_exit = min(closes_list[-(c.n_exit + 1) : -1])

        action: str | None = None

        if self._position == 0:
            if not bear and close > high_enter:
                action = "enter_long"
        elif self._position == 1:
            if close < low_exit:
                action = "exit_long"

        indic = {
            "ma200": round(ma200, 4),
            "bear": bear,
            "high_enter": round(high_enter, 4),
            "low_exit": round(low_exit, 4),
            "n_bars": len(self._closes),
            "position": self._position,
        }
        self._fire_signal(ts_event, action or "NEUTRAL", close, indic)

    def _fire_signal(
        self, ts_event: int, action: str, price: float, indicators: dict | None = None
    ) -> None:
        import asyncio

        strat = self._strategy_id()
        inst = self.config.instrument_id

        audit_body = {
            "strategy": strat,
            "action": action,
            "price": price,
            "bar_ts": ts_event,
            "n_enter": self.config.n_enter,
            "n_exit": self.config.n_exit,
            "bear_ma": self.config.bear_ma,
        }
        rec = sign_signal(audit_body)

        if self._db is not None:
            _indicators = indicators

            async def _store():
                try:
                    sid = await self._db.execute(
                        lambda conn: log_signal(
                            conn,
                            strat,
                            inst,
                            action,
                            price,
                            audit_record_id=rec["record_id"],
                            fingerprint_hex=rec["fingerprint_hex"],
                            sig_b64=rec.get("sig_b64", ""),
                            indicators=_indicators,
                        )
                    )
                    self._pending_signal_id = sid
                except Exception as exc:
                    self.log.error(f"[{strat}] SIGNAL PERSIST FAILED ({action}): {exc}")

            asyncio.ensure_future(_store())

        self._signal_price = price
        self.log.info(
            f"[{strat}] SIGNAL {action} @ {price:.4f}  "
            f"record={rec['record_id']}  tier={rec['tier']}"
        )

        if action == "NEUTRAL":
            return

        # ── portfolio risk gate (pre-trade) — see paper/risk.py ──
        if action.startswith("enter"):
            _dec = RISK.gate_entry(strat, inst, self.config.qty_usd)
            if not _dec.allowed:
                self.log.warning(f"[{strat}] ENTRY BLOCKED by risk: {_dec.reason}")
                import asyncio as _a

                _blk = f"entry blocked: {_dec.reason}"
                _a.ensure_future(
                    self._db.execute(
                        lambda conn: log_risk_event(
                            conn, "block", f"{strat}/{inst}", "warning", _blk
                        )
                    )
                )
                return
            RISK.open_position(strat, inst, self.config.qty_usd)
        else:
            RISK.close_position(strat, inst)

        instrument = self.cache.instrument(
            InstrumentId.from_str(self.config.instrument_id)
        )
        if instrument is None:
            return

        qty = instrument.min_quantity

        if action == "enter_long":
            side = OrderSide.BUY
            self._position = 1
        elif action == "exit_long":
            side = OrderSide.SELL
            self._position = 0
        else:
            return

        order = self.order_factory.market(
            instrument_id=instrument.id,
            order_side=side,
            quantity=qty,
            time_in_force=TimeInForce.IOC,
            client_order_id=next_client_order_id(
                strat
            ),  # OKX-safe alphanumeric clOrdId
        )
        self._order_submit_ns = self.clock.timestamp_ns()
        self.submit_order(order)

    def on_order_filled(self, event: Any) -> None:
        import asyncio

        if self._db is not None and self._signal_price is not None:
            fill_price = float(str(event.last_px))
            side = "BUY" if event.order_side == OrderSide.BUY else "SELL"
            qty = float(str(event.last_qty))
            strat = self._strategy_id()
            inst = self.config.instrument_id
            # Reconcile risk exposure to the ACTUAL executed notional (price×qty),
            # replacing the nominal qty_usd estimate from _fire_signal. _position is
            # already updated by _fire_signal: non-zero = entry fill, 0 = exit fill.
            if self._position != 0:
                RISK.open_position(strat, inst, fill_price * qty)
            else:
                RISK.close_position(strat, inst)
            fill_type = (
                "maker"
                if getattr(event, "liquidity_side", None) == LiquiditySide.MAKER
                else "taker"
            )
            latency_ms = None
            if self._order_submit_ns is not None:
                latency_ms = max(
                    0,
                    int(
                        (self.clock.timestamp_ns() - self._order_submit_ns) / 1_000_000
                    ),
                )

            sig_price = self._signal_price
            sig_id = self._pending_signal_id

            async def _store():
                try:
                    await self._db.execute(
                        lambda conn: log_fill(
                            conn,
                            strat,
                            inst,
                            side,
                            qty,
                            signal_price=sig_price,
                            actual_fill_price=fill_price,
                            order_id=str(event.client_order_id),
                            venue_order_id=str(getattr(event, "venue_order_id", "")),
                            latency_ms=latency_ms,
                            fill_type=fill_type,
                            signal_id=sig_id,
                        )
                    )
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
