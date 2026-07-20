"""Strategy 2: VWAP Mean Reversion 1H (SOL + BTC USDT-SWAP, taker execution).

Backtest reference: R5.3 — SOL 1H gross=+1.155, @10bps=+0.074.
Paper purpose: validate fill slippage vs backtest (signal_price=bar.close, actual=fill).
Parameters: vwap_n=4 (4H window), z_thr=2.0, hold=6 bars (6H).
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
from paper.risk import RISK, gate_entry_dynamic, log_risk_event
from paper.db import DB_DSN, DDL, log_signal, log_fill
from paper.db_pool import ResilientPool
from paper.order_ids import next_client_order_id
from paper.strategies._guard import (
    close_positions_okx_safe,
    own_net_position,
    own_open_qty,
    start_exposure_sync,
    start_manual_close_poll,
    resync_position_from_venue,
    survive,
)
from paper.strategies._sizing import risk_sized_qty_usd


class VwapMR1HConfig(StrategyConfig, frozen=True):
    instrument_id: str
    bar_type: str
    vwap_n: int = 4  # 4H VWAP window
    z_thr: float = 2.0
    hold: int = 6  # bars before time-exit
    qty_usd: float = 200.0
    risk_pct: float = (
        0.0  # >0 → 风险定仓(每笔风险 = base 的 risk_pct%);0 = 固定 qty_usd
    )
    max_qty_usd: float = 1000.0  # 风险定仓的单笔名义上限
    sl_std: float = 1.0  # 止损距离 = sl_std × 入场时收盘σ
    min_rr: float = 1.5  # 止盈 = min_rr × 止损距离 — 结构性保证盈亏比 ≥ min_rr
    cooldown_after_sl: int = 0  # 止损后 N 根 bar 内禁止再入场(0=关);对付单边行情绞肉机


class VwapMR1H(Strategy):
    """VWAP mean reversion 1H via taker market orders."""

    STRATEGY_BASE = "vwap_mr_1h"

    def __init__(self, config: VwapMR1HConfig) -> None:
        super().__init__(config)
        self._closes: deque[float] = deque(maxlen=config.vwap_n + 2)
        self._volumes: deque[float] = deque(maxlen=config.vwap_n + 2)
        self._position: int = 0  # 0=flat, +1=long, -1=short
        self._bars_left: int = 0
        self._entry_px: float | None = (
            None  # SL/TP 括号锚点(rehydrate 仓位无锚点→仅时间平仓)
        )
        self._sl_dist: float | None = None
        self._pending_sl: float | None = None
        self._sl_cooldown: int = 0
        self._signal_price: float | None = None
        self._pending_signal_id: int | None = None
        self._order_submit_ns: int | None = None
        self._db: ResilientPool | None = None

    def _strategy_id(self) -> str:
        inst = self.config.instrument_id.replace(".", "_").replace("-", "_").lower()
        return f"{self.STRATEGY_BASE}_{inst}"

    @survive
    def on_start(self) -> None:
        import asyncio

        self._bar_type = BarType.from_str(self.config.bar_type)
        self.subscribe_bars(self._bar_type)
        start_exposure_sync(self)
        self._db = ResilientPool(DB_DSN, DDL, name=self._strategy_id(), logger=self.log)
        start_manual_close_poll(self)
        asyncio.ensure_future(self._boot())
        asyncio.ensure_future(self._rehydrate_position())

    async def _boot(self) -> None:
        await self._db.ensure()
        await self._seed_warmup()

    async def _seed_warmup(self) -> None:
        # Seed closes+volumes from market_data.ohlcv_1h so a node restart doesn't
        # reset the VWAP window. Seeds HISTORY ONLY — no evaluation; the next live
        # hourly close makes the first real decision.
        sid = self._strategy_id()
        if self._closes:  # live bars already arrived — don't splice history under them
            return
        inst_db = self.config.instrument_id.split(".")[0]
        limit = self._closes.maxlen or 8
        try:
            rows = await self._db.execute(
                lambda conn: conn.fetch(
                    """SELECT close AS c, volume AS v FROM market_data.ohlcv_1h
                     WHERE instrument = $1 AND source = 'okx_swap_1h'
                       AND bar_close_ts <= date_trunc('hour', now())
                     ORDER BY bar_close_ts DESC LIMIT $2""",
                    inst_db,
                    limit,
                )
            )
        except Exception as exc:
            self.log.warning(f"[{sid}] warmup seed skipped: {exc}")
            return
        if not rows:
            self.log.warning(f"[{sid}] warmup seed: no 1h history for {inst_db}")
            return
        for r in reversed(rows):  # chronological
            self._closes.append(float(r["c"]))
            self._volumes.append(float(r["v"]) if r["v"] is not None else 0.0)
        self.log.info(f"[{sid}] warmup seeded {len(rows)} 1h bars from ohlcv_1h")

    async def _rehydrate_position(self) -> None:
        # After a restart the venue may hold a position this strategy opened before
        # the crash; NT reconciles it into the cache at startup. Recover direction
        # so logic state matches reality. Deferred (reconciliation settles) and
        # guarded (only when still flat) so it never overrides a live signal.
        import asyncio

        await asyncio.sleep(10)
        if self._position != 0:
            return
        # 策略级归属;不可归属 → 保持 flat,绝不回退到账户级 net_position(见 _guard)
        net = own_net_position(self)
        if net is None:
            return
        self._position = 1 if net > 0 else (-1 if net < 0 else 0)
        if self._position != 0:
            self._bars_left = self.config.hold
            self.log.info(
                f"[{self._strategy_id()}] rehydrated _position={self._position} from own net={net}"
            )

    @survive
    def on_bar(self, bar: Bar) -> None:
        close = float(bar.close)
        volume = float(bar.volume)
        self._closes.append(close)
        self._volumes.append(volume)
        self.log.info(
            f"[on_bar] {bar.bar_type} close={close:.4f} n={len(self._closes)}"
        )

        # SL/TP 括号(先于时间平仓):SL = sl_std×入场σ,TP = min_rr×SL。
        if self._position != 0 and self._entry_px is not None and self._sl_dist:
            edge = (close - self._entry_px) * self._position  # 有利方向为正
            hit = (
                "stop_loss"
                if edge <= -self._sl_dist
                else "take_profit"
                if edge >= self.config.min_rr * self._sl_dist
                else None
            )
            if hit:
                self._fire_signal(
                    bar,
                    hit,
                    close,
                    {
                        "entry": self._entry_px,
                        "sl_dist": round(self._sl_dist, 6),
                        "edge": round(edge, 6),
                        "position": self._position,
                    },
                )
                return

        # Decrement hold / time-based exit
        if self._position != 0:
            self._bars_left -= 1
            if self._bars_left <= 0:
                self._fire_signal(
                    bar,
                    "time_exit",
                    close,
                    {
                        "n_bars": len(self._closes),
                        "position": self._position,
                        "bars_left": 0,
                    },
                )
                return

        if self._position != 0:
            return

        # Need at least vwap_n+1 bars
        if len(self._closes) < self.config.vwap_n + 1:
            self._fire_signal(
                bar, "NEUTRAL", close, {"n_bars": len(self._closes), "warmup": True}
            )
            return

        c_arr = list(self._closes)
        v_arr = list(self._volumes)

        # VWAP from prior vwap_n bars (shift(1) equivalent)
        prior_c = c_arr[-(self.config.vwap_n + 1) : -1]
        prior_v = v_arr[-(self.config.vwap_n + 1) : -1]
        roll_vc = sum(c * v for c, v in zip(prior_c, prior_v))
        roll_v = sum(prior_v)
        vwap = roll_vc / (roll_v + 1e-10)

        import statistics

        if len(prior_c) < 2:
            self._fire_signal(
                bar, "NEUTRAL", close, {"n_bars": len(self._closes), "warmup": True}
            )
            return
        std = statistics.stdev(prior_c)
        z = (close - vwap) / (std + 1e-10)

        indic = {
            "vwap": round(vwap, 4),
            "std": round(std, 6),
            "z": round(z, 4),
            "n_bars": len(self._closes),
            "position": self._position,
            "bars_left": self._bars_left,
        }
        if self._sl_cooldown > 0:
            self._sl_cooldown -= 1
            indic["sl_cooldown"] = self._sl_cooldown + 1
            self._fire_signal(bar, "NEUTRAL", close, indic)
        elif z > self.config.z_thr:
            self._pending_sl = self.config.sl_std * std
            self._fire_signal(bar, "enter_short", close, indic)
        elif z < -self.config.z_thr:
            self._pending_sl = self.config.sl_std * std
            self._fire_signal(bar, "enter_long", close, indic)
        else:
            self._fire_signal(bar, "NEUTRAL", close, indic)

    def _fire_signal(
        self, bar: Bar, action: str, price: float, indicators: dict | None = None
    ) -> None:
        import asyncio

        strat = self._strategy_id()
        inst = self.config.instrument_id

        audit_body = {
            "strategy": strat,
            "action": action,
            "price": price,
            "bar_ts": bar.ts_event,
            "vwap_n": self.config.vwap_n,
            "z_thr": self.config.z_thr,
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
        qty_usd = risk_sized_qty_usd(
            self.config.risk_pct,
            price,
            self._pending_sl,
            self.config.max_qty_usd,
            self.config.qty_usd,
        )
        if action.startswith("enter"):
            _dec = gate_entry_dynamic(strat, inst, qty_usd)
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
            RISK.open_position(strat, inst, qty_usd)
        else:
            RISK.close_position(strat, inst)

        instrument = self.cache.instrument(
            InstrumentId.from_str(self.config.instrument_id)
        )
        if instrument is None:
            return

        # 名义美元 → 合约张数(OKX SWAP 单位是"张",1 张 = ctVal 个币)。原实现入场
        # 写死 min_quantity,qty_usd 只喂风控 gate 不进订单 — 仓位永远最小 1 张。
        px = price if price > 0 else 1.0
        ct_val = float(instrument.multiplier or 1)
        try:
            qty = instrument.make_qty(qty_usd / (ct_val * px))
        except ValueError:
            qty = instrument.min_quantity
        if qty is None or float(str(qty)) < float(str(instrument.min_quantity)):
            qty = instrument.min_quantity

        if action == "enter_short":
            side = OrderSide.SELL
            self._position = -1
            self._bars_left = self.config.hold
            self._entry_px, self._sl_dist = price, self._pending_sl
        elif action == "enter_long":
            side = OrderSide.BUY
            self._position = 1
            self._bars_left = self.config.hold
            self._entry_px, self._sl_dist = price, self._pending_sl
        elif action in ("time_exit", "stop_loss", "take_profit"):
            side = OrderSide.BUY if self._position == -1 else OrderSide.SELL
            self._position = 0
            self._bars_left = 0
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
            client_order_id=next_client_order_id(
                strat
            ),  # OKX-safe alphanumeric clOrdId
        )
        self._order_submit_ns = self.clock.timestamp_ns()
        self.submit_order(order)

    @survive
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
            # 只在整笔成交后清空:部分成交若提前清掉,后续分片会以 signal_id=NULL 落库,
            # 归因时看起来像"无信号孤儿单"。donchian_4h 实测 20 笔逻辑订单里 8 笔部分成交,
            # 45 笔 fill 中 26 笔因此丢了关联,曾被误判为停机风暴清理单。
            _o = self.cache.order(event.client_order_id)
            if _o is None or _o.is_closed:
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
            self._bars_left = 0
            self._entry_px = self._sl_dist = None

    @survive
    def on_stop(self) -> None:
        inst_id = InstrumentId.from_str(self.config.instrument_id)
        close_positions_okx_safe(self)
        if self._db is not None:
            import asyncio

            asyncio.ensure_future(self._db.close())
            self._db = None
