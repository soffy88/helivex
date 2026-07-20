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


class Scalp5MConfig(StrategyConfig, frozen=True):
    instrument_id: str
    bar_type: str
    vwap_n: int = 12  # 1H rolling window on 5m bars (12 × 5m = 60min)
    z_thr: float = 2.0  # z-score threshold (same as R5)
    hold: int = 6  # bars before time-exit (6 × 5m = 30min)
    qty_usd: float = 50.0  # small notional — known loser, control paper burn
    sl_std: float = 1.0  # 止损距离 = sl_std × 入场时收盘σ
    min_rr: float = 1.5  # 止盈 = min_rr × 止损距离 — 结构性保证盈亏比 ≥ min_rr
    cooldown_after_sl: int = 0  # 止损后 N 根 bar 内禁止再入场(0=关);对付单边行情绞肉机
    entry_enabled: bool = True  # False=退役中:只挡入场,平仓照走(见 on_bar 退役闸)
    # ── maker 实验(R5 假设 97% maker 从未被实测:IOC 市价单结构上只能吃单)──
    maker_mode: bool = False  # True=入场/TP/时间平仓走 post-only 挂单
    maker_timeout_bars: int = 1  # 挂单等这么多根 bar 不成交就撤;入场作废,平仓回退市价
    quote_max_age_ms: int = 2000  # 报价超过这个龄就不挂单、回退市价(防按陈旧价穿价被撤)


class Scalp5M(Strategy):
    """5M VWAP-MR scalper. NO-GO gate — observation only, not deployable.

    Runs paper to measure real fill rate / slippage vs R5 backtest assumptions.
    """

    STRATEGY_BASE = "scalp_5m"

    def __init__(self, config: Scalp5MConfig) -> None:
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
        # maker 模式下正在盘口挂着的单:{"id","action","bars","canceling"}
        self._resting: dict[str, Any] | None = None

    def _strategy_id(self) -> str:
        inst = self.config.instrument_id.replace(".", "_").replace("-", "_").lower()
        return f"{self.STRATEGY_BASE}_{inst}"

    @survive
    def on_start(self) -> None:
        import asyncio

        self._bar_type = BarType.from_str(self.config.bar_type)
        self.subscribe_bars(self._bar_type)
        if self.config.maker_mode:
            # post-only 定价必须贴盘口:挂到对手价会被 venue 以 post-only 拒单,
            # 挂太远则永不成交 —— 两种都测不出成交率。
            self.subscribe_quote_ticks(InstrumentId.from_str(self.config.instrument_id))
        start_exposure_sync(self)
        self.log.info(
            f"[{self._strategy_id()}] started (NO-GO observation) — "
            f"subscribing to {self._bar_type}"
        )
        self._db = ResilientPool(DB_DSN, DDL, name=self._strategy_id(), logger=self.log)
        start_manual_close_poll(self)
        asyncio.ensure_future(self._boot())
        asyncio.ensure_future(self._rehydrate_position())

    async def _boot(self) -> None:
        await self._db.ensure()
        await self._seed_warmup()

    async def _seed_warmup(self) -> None:
        # Seed closes+volumes from market_data.ohlcv_5m so a node restart doesn't
        # reset the VWAP window. Seeds HISTORY ONLY — no evaluation; the next live
        # 5m close makes the first real decision.
        sid = self._strategy_id()
        if self._closes:  # live bars already arrived — don't splice history under them
            return
        inst_db = self.config.instrument_id.split(".")[0]
        limit = self._closes.maxlen or 16
        try:
            rows = await self._db.execute(
                lambda conn: conn.fetch(
                    """SELECT close AS c, volume AS v FROM market_data.ohlcv_5m
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
            self.log.warning(f"[{sid}] warmup seed: no 5m history for {inst_db}")
            return
        for r in reversed(rows):  # chronological
            self._closes.append(float(r["c"]))
            self._volumes.append(float(r["v"]) if r["v"] is not None else 0.0)
        self.log.info(f"[{sid}] warmup seeded {len(rows)} 5m bars from ohlcv_5m")

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

        # ── maker 挂单超时 ── 挂着没成交的单先处理,否则会和新信号打架
        if self._resting is not None and not self._resting["canceling"]:
            self._resting["bars"] += 1
            if self._resting["bars"] >= self.config.maker_timeout_bars:
                self._resting["canceling"] = True
                try:
                    self.cancel_order(self.cache.order(self._resting["id"]))
                    self.log.info(
                        f"[{self._strategy_id()}] 挂单超时撤销 "
                        f"({self._resting['action']}, 等了 {self._resting['bars']} 根)"
                    )
                except Exception as exc:
                    self.log.error(f"[{self._strategy_id()}] 撤单失败: {exc!r}")
                    self._resting = None
            return  # 本根不发新信号,等撤单回调落定

        # SL/TP 括号(先于时间平仓):SL = sl_std×入场σ,TP = min_rr×SL。
        # 时间平仓保留作兜底(先到先出)。
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

        # Time-based exit (checked first, position can still have bars left)
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

        if len(self._closes) < self.config.vwap_n + 1:
            self._fire_signal(
                bar,
                "NEUTRAL",
                close,
                {
                    "n_bars": len(self._closes),
                    "warmup": True,
                },
            )
            return

        c_arr = list(self._closes)
        v_arr = list(self._volumes)

        # VWAP from prior vwap_n bars (shift(1) — mirrors R5 backtest exact logic)
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
            "gate": "NO-GO",  # explicit: R5 cost-killed, observation only
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
            f"record={rec['record_id']}  tier={rec['tier']}  [NO-GO obs]"
        )

        if action == "NEUTRAL":
            return

        # ── 退役闸 ── entry_enabled=False 时只挡入场,time_exit/stop_loss/take_profit
        # 照常提交:R5 实验的两个待测量(滑点 0.82bps、fill_type 100% taker)已有答案,
        # 继续入场只产生确认性噪音,还先到先得吃满单标的 $1000 上限把 scalper_v2 锁死
        # (24h 64 次 block / 0 成交)。不做全量封禁是因为本策略有在场持仓,一刀切会把
        # 它们留成孤儿平仓污染统计(见 docs §8.4)——放行平仓让存量自然排空到 flat。
        if action.startswith("enter") and not self.config.entry_enabled:
            self.log.info(f"[{strat}] RETIRED — entry suppressed ({action})")
            return

        # ── portfolio risk gate (pre-trade) — see paper/risk.py ──
        if action.startswith("enter"):
            _dec = gate_entry_dynamic(strat, inst, self.config.qty_usd)
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
            self.log.error(f"[{strat}] instrument not found in cache")
            return

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

        # ── maker 实验:入场/TP/时间平仓挂 post-only,止损永远吃单 ──
        # 止损不做 maker:挂单不成交 = 敞口无上限地扛着,这正是止损要防的事。
        # 宁可为止损付 taker 费,也不能让"省 3bps"把尾部风险打开。
        px = self._passive_px(instrument, side) if action != "stop_loss" else None
        if px is not None:
            order = self.order_factory.limit(
                instrument_id=instrument.id,
                order_side=side,
                quantity=qty,
                price=px,
                time_in_force=TimeInForce.GTC,
                post_only=True,
                client_order_id=next_client_order_id(strat),
            )
            self._resting = {
                "id": order.client_order_id,
                "action": action,
                "bars": 0,
                "canceling": False,
            }
            kind = f"POST-ONLY @ {px}"
        else:
            order = self.order_factory.market(
                instrument_id=instrument.id,
                order_side=side,
                quantity=qty,
                time_in_force=TimeInForce.IOC,
                client_order_id=next_client_order_id(
                    strat
                ),  # OKX-safe alphanumeric clOrdId
            )
            kind = "MARKET"
        self._order_submit_ns = self.clock.timestamp_ns()
        self.submit_order(order)
        self.log.info(f"[{strat}] ORDER submitted: {side} {qty} {kind}")

    def _passive_px(self, instrument: Any, side: OrderSide) -> Any:
        """post-only 的被动价:买挂买一、卖挂卖一。无盘口→None(调用方回退市价)。

        贴同侧最优价而不是穿价,保证 post-only 不被拒;成交与否交给市场,
        这正是本实验要测的量(R5 假设 97% maker)。
        """
        if not self.config.maker_mode:
            return None
        q = self.cache.quote_tick(InstrumentId.from_str(self.config.instrument_id))
        if q is None:
            self.log.warning(f"[{self._strategy_id()}] 无盘口报价 — 回退市价单")
            return None
        # 陈旧报价比没有报价更危险:按几秒前的价挂出去,post-only 一穿价就被 venue
        # 撤单(实测过一次),而我们还以为单子挂在盘口上等成交。宁可吃单也不挂错价。
        age_ms = (self.clock.timestamp_ns() - q.ts_init) / 1_000_000
        if age_ms > self.config.quote_max_age_ms:
            self.log.warning(
                f"[{self._strategy_id()}] 报价过期 {age_ms:.0f}ms "
                f"(>{self.config.quote_max_age_ms}) — 回退市价单"
            )
            return None
        px = q.bid_price if side == OrderSide.BUY else q.ask_price
        self.log.info(
            f"[{self._strategy_id()}] 挂单定价 {px} "
            f"(bid {q.bid_price}/ask {q.ask_price}, 报价龄 {age_ms:.0f}ms)"
        )
        return px

    @survive
    def on_order_filled(self, event: Any) -> None:
        import asyncio

        if self._resting is not None and event.client_order_id == self._resting["id"]:
            # 只在全部成交时松手 —— 部分成交若清掉 _resting,剩余那部分就脱离
            # 超时管理、永远挂在盘口占着敞口。
            o = self.cache.order(event.client_order_id)
            if o is None or o.is_closed:
                self._resting = None

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
            # Real maker/taker from the venue fill report (was hardcoded taker)
            fill_type = (
                "maker"
                if getattr(event, "liquidity_side", None) == LiquiditySide.MAKER
                else "taker"
            )
            # Submit → fill latency (NT clock, ms)
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
        # 撤单来源有两种,处置相同、只在日志上区分:
        #   ① 我们的超时撤单(canceling=True)
        #   ② venue 撤的 —— post-only 若会穿价,OKX 直接撤单而不是拒单。实测
        #      03:20:00 挂 BUY@76.81,450ms 后就被撤(报价滞后/价格已下行)。
        # 关键:不论谁撤的都必须清 _resting。早先只认 ① 的写法会把死订单留在状态里,
        # 5 分钟后超时逻辑对着 CANCELED 订单再撤一次(Cannot cancel: state is CANCELED),
        # 还因为 return 跳过了那一根的平仓重试 —— 仓位靠下一根碰巧自愈,不是靠设计。
        r = self._resting
        if r is not None and event.client_order_id == r["id"]:
            by_venue = not r["canceling"]
            self._resting = None
            if by_venue:
                self.log.warning(
                    f"[{self._strategy_id()}] 挂单被 venue 撤销(post-only 会穿价)"
                    f" — {r['action']}"
                )
            if r["action"].startswith("enter"):
                # 没进场 — 信号作废,复位到 flat(_fire_signal 已乐观置过 ±1)
                self._position = 0
                self._bars_left = 0
                self._entry_px = self._sl_dist = None
                RISK.close_position(self._strategy_id(), self.config.instrument_id)
                self.log.info(f"[{self._strategy_id()}] 入场挂单未成交 — 信号作废")
            else:
                # 平仓单撤了必须补上,否则仓位悬着没人管 —— 回退市价吃单
                self.log.warning(
                    f"[{self._strategy_id()}] 平仓挂单未成交 — 回退市价 ({r['action']})"
                )
                self._submit_market_exit(r["action"])
            return
        self._handle_order_failure("canceled")

    def _submit_market_exit(self, action: str) -> None:
        """maker 平仓单超时后的 taker 兜底。方向按 venue 上本策略的实际持仓定,
        不用 _position —— 后者此刻已被 _fire_signal 置 0(乐观更新)。"""
        inst_id = InstrumentId.from_str(self.config.instrument_id)
        instrument = self.cache.instrument(inst_id)
        own = own_open_qty(self)
        if instrument is None or own is None or own == 0:
            self.log.info(f"[{self._strategy_id()}] 兜底平仓:已无持仓,跳过")
            return
        order = self.order_factory.market(
            instrument_id=instrument.id,
            order_side=OrderSide.SELL if own > 0 else OrderSide.BUY,
            quantity=instrument.make_qty(abs(own)),
            time_in_force=TimeInForce.IOC,
            client_order_id=next_client_order_id(self._strategy_id()),
        )
        self._order_submit_ns = self.clock.timestamp_ns()
        self.submit_order(order)
        self.log.info(f"[{self._strategy_id()}] 兜底 MARKET 平仓 {abs(own)} ({action})")

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
