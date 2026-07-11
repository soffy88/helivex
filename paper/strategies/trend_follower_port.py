"""Strategy 5 (ported): helixa trend_follower → helivex 3O paper node.

Faithful reimplementation of helixa services/nautilus-trader/strategies/trend_follower
(HELIXA_FUTURES_DESIGN_V1) — the *algorithm*, in helivex's own scaffolding:

  ENTER LONG:  close > Donchian(20) upper  AND  ADX(14) >= adx_entry(20)
               AND  atr_health_min <= ATR/close <= atr_health_max
  ENTER SHORT: close < Donchian(20) lower  AND  ADX(14) >= adx_entry
               AND  health ok
  EXIT LONG:   close < Chandelier long stop (HH(22) - ATR*3)  OR  ADX < adx_exit(15)
               OR  held >= max_holding_days(30)
  EXIT SHORT:  close > Chandelier short stop (LL(22) + ATR*3)  OR  ADX < adx_exit
               OR  held >= max_holding_days

Differences from helixa (documented, honest):
  - helixa read an external regime flag + risk-futures allow flag from Redis. helivex
    has an advisory regime layer (paper.regime_state) that research (646dc71) found
    has no OOS persistence, so it is NOT hard-wired here (kept as one soft input at
    the consensus layer, not an in-strategy hard gate). The load-bearing entry/exit
    (Donchian + ADX gate + ATR health + Chandelier trailing + time stop) is faithful.
  - Indicators are hand-rolled (Wilder ATR/ADX) so the live node has no dependency on
    a specific NautilusTrader indicator API version.

**Gating discipline** (per session rule): DEFAULT `trade_enabled=False` → the strategy
computes + logs signals for measurement/gating but submits NO orders. It does not
touch the paper book until it passes helivex's DSR/PBO gate and a human flips the flag.
This is the "NO-GO → observe only, no orders" contract — distinct from the existing 4
strategies which trade on paper regardless.
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


from paper.strategies._indicators import wilder_atr, wilder_adx, ema, macd


class TrendFollowerPortConfig(StrategyConfig, frozen=True):
    instrument_id: str
    bar_type: str
    donchian_period: int = 20
    adx_period: int = 14
    adx_entry: float = 20.0
    adx_exit: float = 15.0
    chandelier_period: int = 22
    chandelier_mult: float = 3.0
    atr_health_min: float = 0.005
    atr_health_max: float = 0.10
    max_holding_days: int = 30
    qty_usd: float = 200.0
    use_ema: bool = True
    ema_period: int = 50
    use_macd: bool = True
    macd_fast: int = 12
    macd_slow: int = 26
    macd_signal: int = 9
    trade_enabled: bool = False  # NO-GO → observe only; flip True only after gate pass


class TrendFollowerPort(Strategy):
    """helixa trend_follower ported to helivex (observe-gated by default)."""

    STRATEGY_BASE = "trend_follower_port"

    def __init__(self, config: TrendFollowerPortConfig) -> None:
        super().__init__(config)
        maxn = (
            max(
                config.donchian_period,
                config.chandelier_period,
                2 * config.adx_period,
                config.ema_period,
                config.macd_slow + config.macd_signal,
            )
            + 5
        )
        self._highs: deque[float] = deque(maxlen=maxn + 2)
        self._lows: deque[float] = deque(maxlen=maxn + 2)
        self._closes: deque[float] = deque(maxlen=maxn + 2)
        self._position: int = 0
        self._bars_held: int = 0
        self._signal_price: float | None = None
        self._signal_ts: int | None = None
        self._pending_signal_id: int | None = None
        self._order_submit_ns: int | None = None
        self._db: ResilientPool | None = None
        self._snapshot: bool = False  # seed-snapshot eval: log only, never trade

    def _strategy_id(self) -> str:
        inst = self.config.instrument_id.replace(".", "_").replace("-", "_").lower()
        return f"{self.STRATEGY_BASE}_{inst}"

    def on_start(self) -> None:
        import asyncio

        self._bar_type = BarType.from_str(self.config.bar_type)
        self.subscribe_bars(self._bar_type)
        mode = "TRADE" if self.config.trade_enabled else "OBSERVE(no orders)"
        self.log.info(
            f"[{self._strategy_id()}] started [{mode}], bars={self._bar_type}"
        )
        self._db = ResilientPool(DB_DSN, DDL, name=self._strategy_id(), logger=self.log)
        # Warm up ADX/ATR/Donchian from helivex's own daily OHLC (resampled from
        # market_data.ohlcv_1h) instead of a ~45-bar live wait. OKX doesn't serve
        # daily history via internal tick aggregation, so request_bars is a no-op
        # here; a DB seed is the reliable path. Best-effort — never crashes the node.
        asyncio.ensure_future(self._boot())

    async def _boot(self) -> None:
        if self._db is None:
            return
        await self._db.ensure()
        await self._seed_warmup()

    async def _seed_warmup(self) -> None:
        sid = self._strategy_id()
        inst_db = self.config.instrument_id.split(".")[
            0
        ]  # BTC-USDT-SWAP.OKX -> BTC-USDT-SWAP
        limit = self._highs.maxlen or 60
        try:
            rows = await self._db.execute(
                lambda conn: conn.fetch(
                    """SELECT date_trunc('day', bar_close_ts) AS d,
                          max(high) AS h, min(low) AS l,
                          (array_agg(close ORDER BY bar_close_ts DESC))[1] AS c
                     FROM market_data.ohlcv_1h
                     WHERE instrument = $1 AND source = 'okx_swap'
                       AND bar_close_ts < date_trunc('day', now())
                     GROUP BY date_trunc('day', bar_close_ts)
                     ORDER BY date_trunc('day', bar_close_ts) DESC
                     LIMIT $2""",
                    inst_db,
                    limit,
                )
            )
        except Exception as exc:
            self.log.warning(f"[{sid}] warmup seed skipped: {exc}")
            return
        if not rows:
            self.log.warning(f"[{sid}] warmup seed: no daily bars for {inst_db}")
            return
        for r in reversed(rows):  # chronological order into the deques
            self._highs.append(float(r["h"]))
            self._lows.append(float(r["l"]))
            self._closes.append(float(r["c"]))
        self.log.info(f"[{sid}] warmup seeded {len(rows)} daily bars from ohlcv_1h")
        # 新鲜度分流:刚错过的日线收盘(≤30min,重启窗口)且 trade_enabled → 补做
        # 一次真实决策(日线一天只有一次窗口,重启跨过收盘就要再等 24h);否则只记
        # 快照(_snapshot=log only, never trade)。注:ports 无 venue 仓位 rehydrate,
        # 但迄今 0 fills,无陈旧仓位;catch-up 只在 _position==0 时可能开新仓。
        import asyncio as _aio
        import datetime as _dt

        last_close = rows[0]["d"] + _dt.timedelta(days=1)
        age = (_dt.datetime.now(_dt.timezone.utc) - last_close).total_seconds()
        if age <= 1800 and self.config.trade_enabled:
            self.log.info(
                f"[{sid}] catch-up eval: missed daily close {age:.0f}s ago during restart"
            )
            await _aio.sleep(12)  # 让 exec 对账/账户状态先落定
            self._evaluate(self.clock.timestamp_ns())
        else:
            self._snapshot = True
            try:
                self._evaluate(self.clock.timestamp_ns())
            finally:
                self._snapshot = False

    def on_historical_data(self, data: Any) -> None:
        if isinstance(data, Bar):
            self._ingest_bar(data, historical=True)

    def on_bar(self, bar: Bar) -> None:
        self._ingest_bar(bar, historical=False)

    def _ingest_bar(self, bar: Bar, *, historical: bool) -> None:
        self._highs.append(float(bar.high))
        self._lows.append(float(bar.low))
        self._closes.append(float(bar.close))
        if self._position != 0:
            self._bars_held += 1
        # do not fire live signals while replaying history — just warm indicators
        if historical:
            return
        self._evaluate(int(bar.ts_event))

    def _evaluate(self, ts_event: int) -> None:
        c = self.config
        if not self._closes:
            return
        close = float(self._closes[-1])
        highs, lows, closes = list(self._highs), list(self._lows), list(self._closes)

        need = (
            max(
                c.donchian_period,
                c.chandelier_period,
                2 * c.adx_period,
                c.ema_period,
                c.macd_slow + c.macd_signal,
            )
            + 1
        )
        if len(closes) < need:
            self._fire(
                ts_event, "NEUTRAL", close, {"warmup": True, "n_bars": len(closes)}
            )
            return

        # Donchian channel over prior `donchian_period` bars (exclude current)
        don_hi = max(highs[-(c.donchian_period + 1) : -1])
        don_lo = min(lows[-(c.donchian_period + 1) : -1])
        adx = wilder_adx(highs, lows, closes, c.adx_period)
        atr = wilder_atr(highs, lows, closes, c.adx_period)
        if adx is None or atr is None:
            self._fire(ts_event, "NEUTRAL", close, {"warmup": True})
            return
        atr_pct = atr / close if close else 0.0
        # Chandelier trailing stops over prior `chandelier_period` bars
        hh = max(highs[-(c.chandelier_period + 1) : -1])
        ll = min(lows[-(c.chandelier_period + 1) : -1])
        chand_long = hh - atr * c.chandelier_mult
        chand_short = ll + atr * c.chandelier_mult
        health_ok = c.atr_health_min <= atr_pct <= c.atr_health_max

        # optional confluence filters (EMA trend + MACD momentum) — tunable from前端
        ema_v = ema(closes, c.ema_period) if c.use_ema else None
        mac = (
            macd(closes, c.macd_fast, c.macd_slow, c.macd_signal)
            if c.use_macd
            else None
        )
        macd_hist = mac[2] if mac else None
        long_conf = ((not c.use_ema) or ema_v is None or close > ema_v) and (
            (not c.use_macd) or macd_hist is None or macd_hist >= 0
        )
        short_conf = ((not c.use_ema) or ema_v is None or close < ema_v) and (
            (not c.use_macd) or macd_hist is None or macd_hist <= 0
        )

        action: str | None = None
        if self._position == 0:
            if close > don_hi and adx >= c.adx_entry and health_ok and long_conf:
                action = "enter_long"
            elif close < don_lo and adx >= c.adx_entry and health_ok and short_conf:
                action = "enter_short"
        elif self._position == 1:
            if (
                close < chand_long
                or adx < c.adx_exit
                or self._bars_held >= c.max_holding_days
            ):
                action = "exit_long"
        elif self._position == -1:
            if (
                close > chand_short
                or adx < c.adx_exit
                or self._bars_held >= c.max_holding_days
            ):
                action = "exit_short"

        indic = {
            "don_hi": round(don_hi, 4),
            "don_lo": round(don_lo, 4),
            "adx": round(adx, 2),
            "atr_pct": round(atr_pct, 5),
            "chand_long": round(chand_long, 4),
            "chand_short": round(chand_short, 4),
            "health_ok": health_ok,
            "ema": round(ema_v, 4) if ema_v is not None else None,
            "macd_hist": round(macd_hist, 4) if macd_hist is not None else None,
            "bars_held": self._bars_held,
            "position": self._position,
            "n_bars": len(closes),
        }
        self._fire(ts_event, action or "NEUTRAL", close, indic)

    def _fire(
        self, ts_event: int, action: str, price: float, indicators: dict | None = None
    ) -> None:
        import asyncio

        inst = self.config.instrument_id
        strat = self._strategy_id()
        rec = sign_signal(
            {
                "strategy": strat,
                "action": action,
                "price": price,
                "bar_ts": ts_event,
            }
        )

        if self._db is not None:
            _indic = indicators

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
                            indicators=_indic,
                        )
                    )
                    self._pending_signal_id = sid
                except Exception as exc:
                    self.log.error(f"[{strat}] SIGNAL PERSIST FAILED ({action}): {exc}")

            asyncio.ensure_future(_store())

        self._signal_price = price
        self._signal_ts = ts_event
        self.log.info(f"[{strat}] SIGNAL {action} @ {price:.2f} tier={rec['tier']}")

        if action == "NEUTRAL" or self._snapshot:
            return

        # OBSERVE gate: without trade_enabled the ported strategy never submits an
        # order — it only records signals so its edge can be measured/gated first.
        if not self.config.trade_enabled:
            # keep logical position state in sync so exits/time-stops still evaluate
            if action == "enter_long":
                self._position, self._bars_held = 1, 0
            elif action == "enter_short":
                self._position, self._bars_held = -1, 0
            elif action in ("exit_long", "exit_short"):
                self._position, self._bars_held = 0, 0
            self.log.info(f"[{strat}] OBSERVE — no order submitted for {action}")
            return

        self._submit(action)

    def _submit(self, action: str) -> None:
        strat, inst = self._strategy_id(), self.config.instrument_id
        if action.startswith("enter"):
            dec = RISK.gate_entry(strat, inst, self.config.qty_usd)
            if not dec.allowed:
                self.log.warning(f"[{strat}] ENTRY BLOCKED by risk: {dec.reason}")
                import asyncio as _a

                _a.ensure_future(
                    self._db.execute(
                        lambda conn: log_risk_event(
                            conn,
                            "block",
                            f"{strat}/{inst}",
                            "warning",
                            f"entry blocked: {dec.reason}",
                        )
                    )
                )
                return
            RISK.open_position(strat, inst, self.config.qty_usd)
        else:
            RISK.close_position(strat, inst)

        instrument = self.cache.instrument(InstrumentId.from_str(inst))
        if instrument is None:
            self.log.error(f"[{strat}] instrument not in cache")
            return
        qty = instrument.make_qty(self.config.qty_usd / float(self._closes[-1] or 1))
        if qty is None or float(str(qty)) < float(str(instrument.min_quantity)):
            qty = instrument.min_quantity

        if action == "enter_long":
            side, self._position, self._bars_held = OrderSide.BUY, 1, 0
        elif action == "enter_short":
            side, self._position, self._bars_held = OrderSide.SELL, -1, 0
        elif action in ("exit_long", "exit_short"):
            side = OrderSide.SELL if self._position == 1 else OrderSide.BUY
            self._position, self._bars_held = 0, 0
        else:
            return

        order = self.order_factory.market(
            instrument_id=instrument.id,
            order_side=side,
            quantity=qty,
            time_in_force=TimeInForce.IOC,
            client_order_id=next_client_order_id(strat),
        )
        self._order_submit_ns = self.clock.timestamp_ns()
        self.submit_order(order)
        self.log.info(f"[{strat}] ORDER submitted: {side} {qty}")

    def on_order_filled(self, event: Any) -> None:
        import asyncio

        if self._db is None or self._signal_price is None:
            return
        fill_price = float(str(event.last_px))
        side = "BUY" if event.order_side == OrderSide.BUY else "SELL"
        qty = float(str(event.last_qty))
        strat, inst = self._strategy_id(), self.config.instrument_id
        sig_id = self._pending_signal_id
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
                0, int((self.clock.timestamp_ns() - self._order_submit_ns) / 1_000_000)
            )
        sig_price = self._signal_price

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
        if self.config.trade_enabled:
            self.close_all_positions(InstrumentId.from_str(self.config.instrument_id))
        if self._db is not None:
            import asyncio

            asyncio.ensure_future(self._db.close())
            self._db = None
