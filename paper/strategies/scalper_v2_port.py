"""Strategy 6 (ported): helixa intraday_scalper_v2 → helivex 3O paper node.

Faithful reimplementation of helixa services/nautilus-trader/strategies/intraday_scalper_v2
(HELIXA V1.5 §6.6) — a dual-mode ADX-hysteresis scalper on 5m bars:

  Mode switch (hysteresis + cooldown):
    mean_reversion → breakout  when ADX(14) >= adx_enter_breakout(22)
    breakout → mean_reversion  when ADX(14) <  adx_exit_breakout(18)
    no re-switch within mode_switch_cooldown_bars(4)

  Entry (flat, ATR-health ok):
    mean_reversion:  RSI(14) <= rsi_oversold(30) → long ; >= rsi_overbought(70) → short
    breakout:        close > BB(20,2) upper → long ; close < BB lower → short

  Exit (by the mode the position was ENTERED in):
    mean_reversion long:  close >= BB mid  OR  RSI >= rsi_neutral_low(45)
    mean_reversion short: close <= BB mid  OR  RSI <= rsi_neutral_high(55)
    breakout long:  close < (trailing high - ATR*1.5)  OR  ADX < breakout_exit_adx(15)
    breakout short: close > (trailing low  + ATR*1.5)  OR  ADX < breakout_exit_adx
    both: held >= max_holding_bars (4h / 5m = 48)

Same gating discipline as trend_follower_port: trade_enabled=False → OBSERVE, logs
signals only, submits NO orders until a gate PASS + human flip. helixa's external
Redis regime/risk gates are not hard-wired (advisory regime lives at the consensus
layer). RSI is [0,100] here (helixa's Nautilus RSI was [0,1] — thresholds converted).
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
from paper.strategies._guard import (
    close_positions_okx_safe,
    resync_position_from_venue,
    survive,
)
from paper.strategies._indicators import (
    wilder_atr,
    wilder_adx,
    wilder_rsi,
    bollinger,
    ema,
    macd,
)


class ScalperV2PortConfig(StrategyConfig, frozen=True):
    instrument_id: str
    bar_type: str
    bb_period: int = 20
    bb_k: float = 2.0
    rsi_period: int = 14
    adx_period: int = 14
    adx_enter_breakout: float = 22.0
    adx_exit_breakout: float = 18.0
    cooldown_bars: int = 4
    rsi_overbought: float = 70.0
    rsi_oversold: float = 30.0
    rsi_neutral_high: float = 55.0
    rsi_neutral_low: float = 45.0
    trailing_atr_mult: float = 1.5
    breakout_exit_adx: float = 15.0
    max_holding_bars: int = 48  # 4h / 5m
    atr_health_min: float = 0.0005
    atr_health_max: float = 0.05
    qty_usd: float = 50.0
    use_ema: bool = True
    ema_period: int = 50
    use_macd: bool = True
    macd_fast: int = 12
    macd_slow: int = 26
    macd_signal: int = 9
    trade_enabled: bool = False  # NO-GO → observe only; flip True only after gate pass


class ScalperV2Port(Strategy):
    """helixa intraday_scalper_v2 ported to helivex (observe-gated by default)."""

    STRATEGY_BASE = "scalper_v2_port"

    def __init__(self, config: ScalperV2PortConfig) -> None:
        super().__init__(config)
        maxn = (
            max(
                config.bb_period,
                config.rsi_period,
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
        self._mode: str = "mr"  # mr = mean_reversion, bo = breakout
        self._entry_mode: str = "mr"  # mode the current position was entered in
        self._cooldown: int = 0
        self._bars_held: int = 0
        self._trail_extreme: float | None = (
            None  # trailing high(long)/low(short) for bo exit
        )
        self._signal_price: float | None = None
        self._signal_ts: int | None = None
        self._pending_signal_id: int | None = None
        self._order_submit_ns: int | None = None
        self._db: ResilientPool | None = None
        self._snapshot: bool = False  # seed-snapshot eval: log only, never trade

    def _strategy_id(self) -> str:
        inst = self.config.instrument_id.replace(".", "_").replace("-", "_").lower()
        return f"{self.STRATEGY_BASE}_{inst}"

    @survive
    def on_start(self) -> None:
        import asyncio

        self._bar_type = BarType.from_str(self.config.bar_type)
        self.subscribe_bars(self._bar_type)
        mode = "TRADE" if self.config.trade_enabled else "OBSERVE(no orders)"
        self.log.info(
            f"[{self._strategy_id()}] started [{mode}], bars={self._bar_type}"
        )
        self._db = ResilientPool(DB_DSN, DDL, name=self._strategy_id(), logger=self.log)
        asyncio.ensure_future(self._boot())

    async def _boot(self) -> None:
        if self._db is None:
            return
        await self._db.ensure()
        await self._seed_warmup()

    async def _seed_warmup(self) -> None:
        sid = self._strategy_id()
        inst_db = self.config.instrument_id.split(".")[0]
        limit = self._highs.maxlen or 80
        try:
            rows = await self._db.execute(
                lambda conn: conn.fetch(
                    """SELECT high AS h, low AS l, close AS c
                     FROM market_data.ohlcv_5m
                     WHERE instrument = $1 AND source = 'okx_swap_5m'
                     ORDER BY bar_close_ts DESC LIMIT $2""",
                    inst_db,
                    limit,
                )
            )
        except Exception as exc:
            self.log.warning(f"[{sid}] warmup seed skipped: {exc}")
            return
        if not rows:
            self.log.warning(f"[{sid}] warmup seed: no 5m bars for {inst_db}")
            return
        for r in reversed(rows):
            self._highs.append(float(r["h"]))
            self._lows.append(float(r["l"]))
            self._closes.append(float(r["c"]))
        self.log.info(f"[{sid}] warmup seeded {len(rows)} 5m bars from ohlcv_5m")
        self._snapshot = True
        try:
            self._evaluate(self.clock.timestamp_ns())
        finally:
            self._snapshot = False

    @survive
    def on_bar(self, bar: Bar) -> None:
        self._highs.append(float(bar.high))
        self._lows.append(float(bar.low))
        self._closes.append(float(bar.close))
        if self._position != 0:
            self._bars_held += 1
        self._evaluate(int(bar.ts_event))

    def _evaluate(self, ts_event: int) -> None:
        c = self.config
        if not self._closes:
            return
        close = float(self._closes[-1])
        highs, lows, closes = list(self._highs), list(self._lows), list(self._closes)

        need = (
            max(
                c.bb_period,
                c.rsi_period,
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

        adx = wilder_adx(highs, lows, closes, c.adx_period)
        atr = wilder_atr(highs, lows, closes, c.adx_period)
        rsi = wilder_rsi(closes, c.rsi_period)
        bb = bollinger(closes, c.bb_period, c.bb_k)
        if adx is None or atr is None or rsi is None or bb is None:
            self._fire(ts_event, "NEUTRAL", close, {"warmup": True})
            return
        mid, upper, lower = bb
        atr_pct = atr / close if close else 0.0
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

        # ADX-hysteresis mode switch with cooldown
        if self._cooldown > 0:
            self._cooldown -= 1
        else:
            if self._mode == "mr" and adx >= c.adx_enter_breakout:
                self._mode, self._cooldown = "bo", c.cooldown_bars
            elif self._mode == "bo" and adx < c.adx_exit_breakout:
                self._mode, self._cooldown = "mr", c.cooldown_bars

        action: str | None = None
        if self._position == 0:
            if health_ok:
                if self._mode == "mr":
                    # Mean-reversion entries are inherently COUNTER-trend: RSI≤30
                    # (falling price) almost always sits below EMA(50) with negative
                    # MACD hist, so the trend-confluence filter made MR entries
                    # near-impossible — a self-contradiction. Confluence applies to
                    # breakout entries only (where trend alignment makes sense).
                    if rsi <= c.rsi_oversold:
                        action = "enter_long"
                    elif rsi >= c.rsi_overbought:
                        action = "enter_short"
                else:  # breakout — trend confluence (EMA/MACD) applies here
                    if close > upper and long_conf:
                        action = "enter_long"
                    elif close < lower and short_conf:
                        action = "enter_short"
        elif self._position == 1:
            if self._entry_mode == "mr":
                if close >= mid or rsi >= c.rsi_neutral_low:
                    action = "exit_long"
            else:
                self._trail_extreme = max(self._trail_extreme or close, close)
                if (
                    close < self._trail_extreme - atr * c.trailing_atr_mult
                    or adx < c.breakout_exit_adx
                ):
                    action = "exit_long"
            if action is None and self._bars_held >= c.max_holding_bars:
                action = "exit_long"
        elif self._position == -1:
            if self._entry_mode == "mr":
                if close <= mid or rsi <= c.rsi_neutral_high:
                    action = "exit_short"
            else:
                self._trail_extreme = min(self._trail_extreme or close, close)
                if (
                    close > self._trail_extreme + atr * c.trailing_atr_mult
                    or adx < c.breakout_exit_adx
                ):
                    action = "exit_short"
            if action is None and self._bars_held >= c.max_holding_bars:
                action = "exit_short"

        indic = {
            "mode": self._mode,
            "adx": round(adx, 2),
            "rsi": round(rsi, 1),
            "atr_pct": round(atr_pct, 5),
            "bb_mid": round(mid, 4),
            "bb_up": round(upper, 4),
            "bb_lo": round(lower, 4),
            "health_ok": health_ok,
            "ema": round(ema_v, 4) if ema_v is not None else None,
            "macd_hist": round(macd_hist, 4) if macd_hist is not None else None,
            "bars_held": self._bars_held,
            "position": self._position,
            "entry_mode": self._entry_mode,
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
            {"strategy": strat, "action": action, "price": price, "bar_ts": ts_event}
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
        self.log.info(
            f"[{strat}] SIGNAL {action} @ {price:.2f} mode={self._mode} tier={rec['tier']}"
        )

        if action == "NEUTRAL" or self._snapshot:
            return

        if not self.config.trade_enabled:
            # keep logical state coherent so exits/time-stops still evaluate
            if action == "enter_long":
                (
                    self._position,
                    self._bars_held,
                    self._entry_mode,
                    self._trail_extreme,
                ) = 1, 0, self._mode, price
            elif action == "enter_short":
                (
                    self._position,
                    self._bars_held,
                    self._entry_mode,
                    self._trail_extreme,
                ) = -1, 0, self._mode, price
            elif action in ("exit_long", "exit_short"):
                self._position, self._bars_held, self._trail_extreme = 0, 0, None
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
        # 名义美元 → 合约张数:OKX SWAP 数量单位是"张",1 张 = multiplier(ctVal)个币
        # (BTC 0.01 / ETH 0.1 / SOL 1)。少乘 ctVal 会把张数少算 1/ctVal 倍,BTC 上
        # make_qty 因取整到 0 抛 ValueError(2026-07-11 15:00 enter_long 实测被拦)。
        px = float(self._closes[-1] or 1)
        ct_val = float(instrument.multiplier or 1)
        try:
            qty = instrument.make_qty(self.config.qty_usd / (ct_val * px))
        except ValueError:
            qty = instrument.min_quantity
        if qty is None or float(str(qty)) < float(str(instrument.min_quantity)):
            qty = instrument.min_quantity

        if action == "enter_long":
            side, self._position, self._bars_held = OrderSide.BUY, 1, 0
            self._entry_mode, self._trail_extreme = (
                self._mode,
                float(str(self._closes[-1])),
            )
        elif action == "enter_short":
            side, self._position, self._bars_held = OrderSide.SELL, -1, 0
            self._entry_mode, self._trail_extreme = (
                self._mode,
                float(str(self._closes[-1])),
            )
        elif action in ("exit_long", "exit_short"):
            side = OrderSide.SELL if self._position == 1 else OrderSide.BUY
            self._position, self._bars_held, self._trail_extreme = 0, 0, None
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

    @survive
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

    @survive
    def on_order_rejected(self, event: Any) -> None:
        self._handle_order_failure("rejected")

    @survive
    def on_order_denied(self, event: Any) -> None:
        self._handle_order_failure("denied")

    @survive
    def on_order_canceled(self, event: Any) -> None:
        # 本策略从不主动撤单 — cancel 只可能是 IOC 未成交,按拒单重同步
        self._handle_order_failure("canceled")

    @survive
    def on_order_expired(self, event: Any) -> None:
        self._handle_order_failure("expired")

    def _handle_order_failure(self, kind: str) -> None:
        if resync_position_from_venue(self, kind) == 0:
            self._bars_held, self._trail_extreme = 0, None

    @survive
    def on_stop(self) -> None:
        if self.config.trade_enabled:
            close_positions_okx_safe(self)
        if self._db is not None:
            import asyncio

            asyncio.ensure_future(self._db.close())
            self._db = None
