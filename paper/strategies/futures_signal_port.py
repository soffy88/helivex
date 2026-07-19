"""Strategy 7 (ported): helixa futures-signal-engine → helivex 3O paper node.

Faithful reimplementation of helixa services/futures-signal-engine — a breakout +
volume-surge + RSI-band directional signal generator:

  LONG:  close > high(breakout_period=20)  AND  vol > vol_surge_mult(1.5) * volMA(20)
         AND  rsi_long_min(45) <= RSI(14) <= rsi_long_max(75)
  SHORT: close < low(20)                    AND  vol surge
         AND  rsi_short_min(25) <= RSI(14) <= rsi_short_max(55)

Differences from helixa (documented, honest):
  - helixa's engine was signal-ONLY: it emitted a direction gated by an upstream SPOT
    consensus signal (RabbitMQ signals.consensus.spot); position management lived
    downstream. helivex's consensus gate is architectural (only promoted engines drive
    the executable consensus at the consensus layer), so it is NOT re-read in-strategy.
    A symmetric breakout/RSI exit is added here to make it a self-contained strategy.
  - Runs on 1H SWAP bars (helivex has ohlcv_1h; helixa read exchange OHLCV directly).

Gating discipline: trade_enabled=False → OBSERVE, logs signals only, submits NO orders
until a gate PASS + human flip. Same NO-GO contract as the other ported strategies.
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
    own_open_qty,
    start_exposure_sync,
    resync_position_from_venue,
    survive,
)
from paper.strategies._indicators import wilder_atr, wilder_rsi, ema, macd
from paper.strategies._sizing import risk_sized_qty_usd


class FuturesSignalPortConfig(StrategyConfig, frozen=True):
    instrument_id: str
    bar_type: str
    breakout_period: int = 20
    vol_ma_period: int = 20
    vol_surge_mult: float = 1.5
    rsi_period: int = 14
    rsi_long_min: float = 45.0
    rsi_long_max: float = 75.0
    rsi_short_min: float = 25.0
    rsi_short_max: float = 55.0
    qty_usd: float = 100.0
    # SL/TP 括号:原策略只靠反向突破/RSI 离场,一笔逆势单可无限扛 — 加 ATR 止损
    # + min_rr 止盈,结构性锁定盈亏比 ≥ 1:min_rr
    atr_period: int = 14
    sl_atr_mult: float = 2.0  # 止损距离 = sl_atr_mult × 入场 ATR
    min_rr: float = 1.5  # 止盈 = min_rr × 止损距离
    risk_pct: float = (
        0.0  # >0 → 风险定仓(每笔风险 = base 的 risk_pct%);0 = 固定 qty_usd
    )
    max_qty_usd: float = 1000.0  # 风险定仓的单笔名义上限
    use_ema: bool = True
    ema_period: int = 50
    use_macd: bool = True
    macd_fast: int = 12
    macd_slow: int = 26
    macd_signal: int = 9
    trade_enabled: bool = False  # NO-GO → observe only; flip True only after gate pass


class FuturesSignalPort(Strategy):
    """helixa futures-signal-engine ported to helivex (observe-gated by default)."""

    STRATEGY_BASE = "futures_signal_port"

    def __init__(self, config: FuturesSignalPortConfig) -> None:
        super().__init__(config)
        maxn = (
            max(
                config.breakout_period,
                config.vol_ma_period,
                config.rsi_period,
                config.ema_period,
                config.macd_slow + config.macd_signal,
            )
            + 5
        )
        self._highs: deque[float] = deque(maxlen=maxn + 2)
        self._lows: deque[float] = deque(maxlen=maxn + 2)
        self._closes: deque[float] = deque(maxlen=maxn + 2)
        self._vols: deque[float] = deque(maxlen=maxn + 2)
        self._position: int = 0
        self._entry_px: float | None = None  # SL/TP 括号锚点(rehydrate 无锚点→原出场)
        self._sl_dist: float | None = None
        self._pending_sl: float | None = None
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
        start_exposure_sync(self)
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
        limit = self._highs.maxlen or 60
        try:
            rows = await self._db.execute(
                lambda conn: conn.fetch(
                    """SELECT high AS h, low AS l, close AS c, volume AS v
                     FROM market_data.ohlcv_1h
                     WHERE instrument = $1 AND source = 'okx_swap_1h'
                     ORDER BY bar_close_ts DESC LIMIT $2""",
                    inst_db,
                    limit,
                )
            )
        except Exception as exc:
            self.log.warning(f"[{sid}] warmup seed skipped: {exc}")
            return
        if not rows:
            self.log.warning(f"[{sid}] warmup seed: no 1h bars for {inst_db}")
            return
        for r in reversed(rows):
            self._highs.append(float(r["h"]))
            self._lows.append(float(r["l"]))
            self._closes.append(float(r["c"]))
            self._vols.append(float(r["v"]) if r["v"] is not None else 0.0)
        self.log.info(f"[{sid}] warmup seeded {len(rows)} 1h bars from ohlcv_1h")
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
        self._vols.append(float(bar.volume))
        self._evaluate(int(bar.ts_event))

    def _evaluate(self, ts_event: int) -> None:
        c = self.config
        if not self._closes:
            return
        close = float(self._closes[-1])
        highs, lows, closes, vols = (
            list(self._highs),
            list(self._lows),
            list(self._closes),
            list(self._vols),
        )

        need = (
            max(
                c.breakout_period,
                c.vol_ma_period,
                c.rsi_period,
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

        high_bo = max(
            highs[-(c.breakout_period + 1) : -1]
        )  # prior breakout_period bars
        low_bo = min(lows[-(c.breakout_period + 1) : -1])
        vol_ma = sum(vols[-(c.vol_ma_period + 1) : -1]) / c.vol_ma_period
        vol_now = vols[-1]
        rsi = wilder_rsi(closes, c.rsi_period)
        if rsi is None:
            self._fire(ts_event, "NEUTRAL", close, {"warmup": True})
            return
        vol_surge = vol_ma > 0 and vol_now > c.vol_surge_mult * vol_ma
        atr = wilder_atr(highs, lows, closes, c.atr_period)
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
            if (
                close > high_bo
                and vol_surge
                and c.rsi_long_min <= rsi <= c.rsi_long_max
                and long_conf
            ):
                action = "enter_long"
            elif (
                close < low_bo
                and vol_surge
                and c.rsi_short_min <= rsi <= c.rsi_short_max
                and short_conf
            ):
                action = "enter_short"
        elif self._position == 1:
            # SL/TP 括号优先(结构性盈亏比 ≥ 1:min_rr);rehydrate 无锚点时退回原出场
            if self._entry_px is not None and self._sl_dist:
                if close <= self._entry_px - self._sl_dist:
                    action = "exit_long"
                elif close >= self._entry_px + c.min_rr * self._sl_dist:
                    action = "exit_long"
            # symmetric exit: opposite breakout or RSI leaves the long band (added for a
            # self-contained strategy; helixa's engine was signal-only)
            if action is None and (close < low_bo or rsi > c.rsi_long_max):
                action = "exit_long"
        elif self._position == -1:
            if self._entry_px is not None and self._sl_dist:
                if close >= self._entry_px + self._sl_dist:
                    action = "exit_short"
                elif close <= self._entry_px - c.min_rr * self._sl_dist:
                    action = "exit_short"
            if action is None and (close > high_bo or rsi < c.rsi_short_min):
                action = "exit_short"

        if action in ("enter_long", "enter_short"):
            self._pending_sl = atr * c.sl_atr_mult if atr else None

        indic = {
            "high_bo": round(high_bo, 4),
            "low_bo": round(low_bo, 4),
            "vol_ma": round(vol_ma, 2),
            "vol_now": round(vol_now, 2),
            "vol_surge": vol_surge,
            "rsi": round(rsi, 1),
            "atr": round(atr, 4) if atr is not None else None,
            "ema": round(ema_v, 4) if ema_v is not None else None,
            "macd_hist": round(macd_hist, 4) if macd_hist is not None else None,
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
        self.log.info(f"[{strat}] SIGNAL {action} @ {price:.2f} tier={rec['tier']}")

        if action == "NEUTRAL" or self._snapshot:
            return

        if not self.config.trade_enabled:
            if action == "enter_long":
                self._position = 1
                self._entry_px, self._sl_dist = price, self._pending_sl
            elif action == "enter_short":
                self._position = -1
                self._entry_px, self._sl_dist = price, self._pending_sl
            elif action in ("exit_long", "exit_short"):
                self._position = 0
                self._entry_px = self._sl_dist = None
            self.log.info(f"[{strat}] OBSERVE — no order submitted for {action}")
            return

        self._submit(action)

    def _submit(self, action: str) -> None:
        strat, inst = self._strategy_id(), self.config.instrument_id
        qty_usd = risk_sized_qty_usd(
            self.config.risk_pct,
            float(self._closes[-1] or 0),
            self._pending_sl,
            self.config.max_qty_usd,
            self.config.qty_usd,
        )
        if action.startswith("enter"):
            dec = RISK.gate_entry(strat, inst, qty_usd)
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
            RISK.open_position(strat, inst, qty_usd)
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
            qty = instrument.make_qty(qty_usd / (ct_val * px))
        except ValueError:
            qty = instrument.min_quantity
        if qty is None or float(str(qty)) < float(str(instrument.min_quantity)):
            qty = instrument.min_quantity

        if action == "enter_long":
            side, self._position = OrderSide.BUY, 1
            self._entry_px, self._sl_dist = px, self._pending_sl
        elif action == "enter_short":
            side, self._position = OrderSide.SELL, -1
            self._entry_px, self._sl_dist = px, self._pending_sl
        elif action in ("exit_long", "exit_short"):
            side = OrderSide.SELL if self._position == 1 else OrderSide.BUY
            self._position = 0
            self._entry_px = self._sl_dist = None
            # 平仓用本策略实际持仓量,避免按现价重算导致的数量漂移残渣
            _own = own_open_qty(self)
            if _own is not None:
                qty = instrument.make_qty(abs(_own))

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
            _i = self.cache.instrument(InstrumentId.from_str(inst))
            _ct = float(_i.multiplier) if _i is not None else 1.0
            RISK.open_position(strat, inst, fill_price * qty * _ct)
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
        resync_position_from_venue(self, "rejected")

    @survive
    def on_order_denied(self, event: Any) -> None:
        resync_position_from_venue(self, "denied")

    @survive
    def on_order_canceled(self, event: Any) -> None:
        # 本策略从不主动撤单 — cancel 只可能是 IOC 未成交,按拒单重同步
        resync_position_from_venue(self, "canceled")

    @survive
    def on_order_expired(self, event: Any) -> None:
        resync_position_from_venue(self, "expired")

    @survive
    def on_stop(self) -> None:
        if self.config.trade_enabled:
            close_positions_okx_safe(self)
        if self._db is not None:
            import asyncio

            asyncio.ensure_future(self._db.close())
            self._db = None
