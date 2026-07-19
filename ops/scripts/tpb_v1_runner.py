"""TPB-001 trend_pullback_v1 — 自写盘内回测 runner(HELIVEX-IMPL_SPEC-TPB-001)。

为什么不用 helios_backtest_v51.simulate():该函数是 close-to-close
(simulator.py:65 `bar_pnl = position_fraction * bar.close_return`),表达不了
本策略的三个核心机制 —— maker 限价成交判定、止损盘中穿越、同 bar SL 优先于 TP。
v51 只复用其 CPCV/PBO/DSR 验证层(见 tpb_v1_gate.py)。

前视禁令(spec N2)在本文件的落实:
  - 4h 指标 asof 对齐到 1h,方向 backward(1h bar 在 t 只看 bar_close_ts ≤ t 的 4h bar)
  - Ichimoku 云天然前移 26 期(senkou 在 bar i 的值来自 bar i-26)→ 结构性无前视
  - 限价在 bar t 收盘挂出,成交判定用 bar t+1 的 low/high
  - ATR_entry 冻结为挂单时刻(bar t)的 ATR —— 成交发生在 t+1 盘中,那时 t+1 的
    ATR 尚不可知,用它就是前视
  - 吊灯止损在 bar t 收盘更新,供 bar t+1 盘中检查

确定性(spec N4):无随机成分,同数据同配置必得同结果。

用法:
    python ops/scripts/tpb_v1_runner.py --once      # 中心配置单次运行
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import os
from dataclasses import dataclass, field, asdict
from pathlib import Path

import asyncpg
import numpy as np
import pandas as pd

HV_DSN = os.environ.get(
    "DB_DSN", "postgresql://helios:helios_dev_pass@localhost:5434/helivex"
)
HELIOS_DSN = os.environ.get(
    "HELIOS_DSN", "postgresql://helios:helios_dev_pass@localhost:5434/helios"
)

# ── 冻结常量(spec Part II,禁止运行时修改)────────────────────────────────
INSTRUMENTS = ["BTC-USDT-SWAP", "ETH-USDT-SWAP", "SOL-USDT-SWAP"]
SRC_4H = "okx_swap"  # F2: 该 source 真实周期是 4h(14400s),命名误导
SRC_1H = "okx_swap_1h"  # F2: 真 1h(3600s)
WINDOW_START = dt.datetime(2024, 7, 20, 1, 0, tzinfo=dt.timezone.utc)
WINDOW_END = dt.datetime(2026, 7, 10, 0, 0, tzinfo=dt.timezone.utc)
FUNDING_REAL_FROM = dt.datetime(2025, 5, 7, tzinfo=dt.timezone.utc)  # dydx 起点

MAKER_BPS = 2.0  # C1/C2
TAKER_BPS = 5.0  # C3
SLIP_BPS = 1.0  # C3/C4
FUNDING_PROXY_PER_H = 0.0001 / 8.0  # F10: 1bp/8h,无论方向均为支付

RISK_PCT = 0.005  # P1
GROSS_CAP_MULT = 1.0  # P2
CONSEC_LOSS_HALT = 3  # P3
HALT_HOURS = 24  # P3
DAILY_LOSS_HALT = 0.02  # P3
INITIAL_EQUITY = 10_000.0

MAX_HOLD_BARS = 48  # X4
TP_R_MULT = 2.0  # X2: +2R
TP_FRACTION = 0.5  # X2: 平 50%


@dataclass(frozen=True)
class TPBConfig:
    """§4 预注册参数。中心配置为默认值;T3 扰动只改 adx_entry/sl_atr/trail_atr。"""

    adx_entry: float = 25.0  # RG2
    sl_atr: float = 2.0  # X1
    trail_atr: float = 3.0  # X3
    label: str = "center"


@dataclass
class Trade:
    instrument: str
    side: int  # +1 long, -1 short
    entry_ts: dt.datetime
    entry_px: float
    qty: float
    atr_entry: float
    exit_ts: dt.datetime | None = None
    exit_px: float | None = None
    exit_reason: str = ""
    gross_pnl: float = 0.0
    maker_cost: float = 0.0
    taker_cost: float = 0.0
    funding_cost: float = 0.0
    bars_held: int = 0
    tp_filled: bool = False

    @property
    def net_pnl(self) -> float:
        return self.gross_pnl - self.maker_cost - self.taker_cost - self.funding_cost


# ── 指标(向量化,Wilder 平滑用 ewm(alpha=1/n, adjust=False))──────────────


def wilder_atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    prev_close = df["close"].shift()
    tr = pd.concat(
        [
            df["high"] - df["low"],
            (df["high"] - prev_close).abs(),
            (df["low"] - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return tr.ewm(alpha=1.0 / period, adjust=False).mean()


def wilder_adx(df: pd.DataFrame, period: int = 14) -> pd.Series:
    up = df["high"].diff()
    dn = -df["low"].diff()
    plus_dm = pd.Series(np.where((up > dn) & (up > 0), up, 0.0), index=df.index)
    minus_dm = pd.Series(np.where((dn > up) & (dn > 0), dn, 0.0), index=df.index)
    atr = wilder_atr(df, period)
    pdi = (
        100
        * plus_dm.ewm(alpha=1.0 / period, adjust=False).mean()
        / atr.replace(0, np.nan)
    )
    mdi = (
        100
        * minus_dm.ewm(alpha=1.0 / period, adjust=False).mean()
        / atr.replace(0, np.nan)
    )
    dx = 100 * (pdi - mdi).abs() / (pdi + mdi).replace(0, np.nan)
    return dx.ewm(alpha=1.0 / period, adjust=False).mean()


def ichimoku_cloud(df: pd.DataFrame) -> tuple[pd.Series, pd.Series]:
    """标准 9/26/52。senkou 前移 26 期 → bar i 看到的云来自 bar i-26,结构性无前视。"""
    tenkan = (df["high"].rolling(9).max() + df["low"].rolling(9).min()) / 2
    kijun = (df["high"].rolling(26).max() + df["low"].rolling(26).min()) / 2
    senkou_a = ((tenkan + kijun) / 2).shift(26)
    senkou_b = ((df["high"].rolling(52).max() + df["low"].rolling(52).min()) / 2).shift(
        26
    )
    return senkou_a, senkou_b


def kijun_line(df: pd.DataFrame, period: int = 26) -> pd.Series:
    return (df["high"].rolling(period).max() + df["low"].rolling(period).min()) / 2


# ── 数据加载 ────────────────────────────────────────────────────────────────


async def load_ohlcv(
    conn: asyncpg.Connection,
    inst: str,
    source: str,
    start: dt.datetime,
    end: dt.datetime,
) -> pd.DataFrame:
    rows = await conn.fetch(
        """SELECT bar_close_ts AS ts, open, high, low, close, volume
           FROM market_data.ohlcv_1h
           WHERE instrument=$1 AND source=$2 AND bar_close_ts >= $3 AND bar_close_ts <= $4
           ORDER BY bar_close_ts ASC""",
        inst,
        source,
        start,
        end,
    )
    df = pd.DataFrame(
        [
            (
                r["ts"],
                float(r["open"]),
                float(r["high"]),
                float(r["low"]),
                float(r["close"]),
                float(r["volume"] or 0),
            )
            for r in rows
        ],
        columns=["ts", "open", "high", "low", "close", "volume"],
    )
    return df.set_index("ts")


async def load_funding(conn: asyncpg.Connection) -> dict[str, pd.Series]:
    """dydx 1h 费率(小数)。asset 是 BTC/ETH/SOL,映射回 instrument。"""
    out: dict[str, pd.Series] = {}
    for inst in INSTRUMENTS:
        asset = inst.split("-")[0]
        rows = await conn.fetch(
            """SELECT ts, value FROM public.raw_features
               WHERE feature_name='funding_rate' AND source='dydx' AND asset=$1
               ORDER BY ts ASC""",
            asset,
        )
        if rows:
            s = pd.Series(
                [float(r["value"]) for r in rows],
                index=pd.DatetimeIndex([r["ts"] for r in rows]),
            )
            # dydx 采样在 :50 附近。resample 的标签是 bin 起点(13:00 代表
            # 13:00-14:00),而 1h bar 的索引是 bar_close_ts(14:00 代表同一段)。
            # 不 +1h 会把费率错配到前一根 bar。
            r1 = s.resample("1h").mean()
            r1.index = r1.index + pd.Timedelta(hours=1)
            out[inst] = r1
        else:
            out[inst] = pd.Series(dtype=float)
    return out


async def build_dataset(cfg: TPBConfig) -> dict[str, pd.DataFrame]:
    """每资产一张 1h 表,含已 asof 对齐的 4h regime 列。"""
    hv = await asyncpg.connect(HV_DSN)
    he = await asyncpg.connect(HELIOS_DSN)
    try:
        funding = await load_funding(he)
        data: dict[str, pd.DataFrame] = {}
        # 4h 需要 warmup:Ichimoku 52+26 位移 = 78 根,取 120 根余量
        warm_4h = WINDOW_START - dt.timedelta(hours=4 * 120)
        warm_1h = WINDOW_START - dt.timedelta(hours=60)  # Kijun26/ATR14 余量
        for inst in INSTRUMENTS:
            d4 = await load_ohlcv(hv, inst, SRC_4H, warm_4h, WINDOW_END)
            d1 = await load_ohlcv(hv, inst, SRC_1H, warm_1h, WINDOW_END)
            if d4.empty or d1.empty:
                raise RuntimeError(f"{inst}: 数据缺失 4h={len(d4)} 1h={len(d1)}")

            sa, sb = ichimoku_cloud(d4)
            r4 = pd.DataFrame(
                {
                    "cloud_top": pd.concat([sa, sb], axis=1).max(axis=1),
                    "cloud_bot": pd.concat([sa, sb], axis=1).min(axis=1),
                    "adx4": wilder_adx(d4, 14),
                    "close4": d4["close"],
                }
            )

            d1 = d1.copy()
            d1["kijun"] = kijun_line(d1, 26)
            d1["atr"] = wilder_atr(d1, 14)

            # N2: asof backward — 1h bar 在 t 只用 bar_close_ts ≤ t 的 4h bar
            merged = pd.merge_asof(
                d1.reset_index().sort_values("ts"),
                r4.reset_index().rename(columns={"index": "ts"}).sort_values("ts"),
                on="ts",
                direction="backward",
                allow_exact_matches=True,
            ).set_index("ts")

            fs = funding.get(inst, pd.Series(dtype=float))
            merged["funding"] = fs.reindex(merged.index) if len(fs) else np.nan

            # 主窗口裁剪(warmup 段只用于指标预热,不产生交易)
            merged = merged[
                (merged.index >= WINDOW_START) & (merged.index <= WINDOW_END)
            ]
            data[inst] = merged
        return data
    finally:
        await hv.close()
        await he.close()


# ── 事件驱动组合模拟 ────────────────────────────────────────────────────────


@dataclass
class Position:
    side: int
    entry_px: float
    qty: float
    atr_entry: float
    entry_ts: dt.datetime
    eff_stop: float  # 有效止损,单调(多头只升/空头只降)
    tp_px: float
    tp_filled: bool = False
    extreme_close: float = 0.0  # 入场以来最高(多)/最低(空)close
    mfe_r: float = 0.0  # 最大有利偏移,单位 R
    bars_held: int = 0
    trade: Trade = None  # type: ignore


class PortfolioSim:
    """三资产共享权益的事件驱动组合(spec P5)。"""

    def __init__(self, cfg: TPBConfig, data: dict[str, pd.DataFrame]):
        self.cfg = cfg
        self.data = data
        self.equity = INITIAL_EQUITY
        # 仪表(不参与任何交易决策):gross 权益曲线供 T2 KILL-1 的 gross Sharpe,
        # 挂单计数供 T6 maker 成交率
        self.gross_equity = INITIAL_EQUITY
        self.gross_curve: list[tuple[dt.datetime, float]] = []
        self.orders_placed = 0
        self.n_fills = 0
        self._cur: dict = {}   # 本时间戳各资产的 bar(run() 每步填充)
        self.pos: dict[str, Position | None] = {i: None for i in INSTRUMENTS}
        self.pending: dict[str, float | None] = {i: None for i in INSTRUMENTS}
        self.pending_atr: dict[str, float] = {i: 0.0 for i in INSTRUMENTS}
        self.pending_side: dict[str, int] = {i: 0 for i in INSTRUMENTS}
        self.trades: list[Trade] = []
        self.equity_curve: list[tuple[dt.datetime, float]] = []
        self.consec_losses = 0
        self.halt_until: dt.datetime | None = None
        self.day_start_equity = INITIAL_EQUITY
        self.cur_day: dt.date | None = None
        self.capacity_events = 0
        self.touch_rejects = 0  # T6: 被 E5 严格判定拒掉的 touch 数

    # ---- 风控 ----
    def _halted(self, ts: dt.datetime) -> bool:
        return self.halt_until is not None and ts < self.halt_until

    def _roll_day(self, ts: dt.datetime) -> None:
        d = ts.date()
        if self.cur_day is None:
            self.cur_day, self.day_start_equity = d, self.equity
        elif d != self.cur_day:
            self.cur_day, self.day_start_equity = d, self.equity
            if self.halt_until is not None and self.halt_until <= ts:
                self.halt_until = None

    def _check_daily_loss(self, ts: dt.datetime) -> None:
        if self.equity < self.day_start_equity * (1 - DAILY_LOSS_HALT):
            nxt = dt.datetime.combine(
                ts.date() + dt.timedelta(days=1), dt.time.min, tzinfo=dt.timezone.utc
            )
            if self.halt_until is None or nxt > self.halt_until:
                self.halt_until = nxt

    def _gross_notional(self, prices: dict[str, float]) -> float:
        tot = 0.0
        for i, p in self.pos.items():
            if p is not None:
                tot += abs(p.qty) * prices.get(i, p.entry_px)
        return tot

    # ---- 出场 ----
    def _close(
        self,
        inst: str,
        ts: dt.datetime,
        px: float,
        reason: str,
        taker: bool,
        fraction: float = 1.0,
    ) -> None:
        p = self.pos[inst]
        if p is None:
            return
        qty = abs(p.qty) * fraction
        gross = (px - p.entry_px) * p.side * qty
        cost_bps = TAKER_BPS if taker else MAKER_BPS
        cost = px * qty * cost_bps / 10_000.0
        t = p.trade
        t.gross_pnl += gross
        if taker:
            t.taker_cost += cost
        else:
            t.maker_cost += cost
        self.equity += gross - cost
        self.gross_equity += gross

        if fraction >= 1.0:
            t.exit_ts, t.exit_px, t.exit_reason = ts, px, reason
            t.bars_held = p.bars_held
            self.trades.append(t)
            self.pos[inst] = None
            if t.net_pnl < 0:
                self.consec_losses += 1
                if self.consec_losses >= CONSEC_LOSS_HALT:
                    self.halt_until = ts + dt.timedelta(hours=HALT_HOURS)
                    self.consec_losses = 0
            else:
                self.consec_losses = 0
        else:
            p.qty -= p.qty * fraction
            p.tp_filled = True
            t.tp_filled = True
            p.eff_stop = p.entry_px  # X2: TP 后止损上移 breakeven
        self._check_daily_loss(ts)

    def _manage(self, inst: str, ts: dt.datetime, row) -> None:
        """出场检查。X6:SL 优先于 TP。止损/吊灯用【上一根收盘时】算好的 eff_stop。"""
        p = self.pos[inst]
        if p is None:
            return
        p.bars_held += 1
        hi, lo, cl = row.high, row.low, row.close

        # X6 SL 优先
        if p.side > 0 and lo <= p.eff_stop:
            self._close(
                inst,
                ts,
                p.eff_stop * (1 - SLIP_BPS / 10_000.0),
                "stop" if not p.tp_filled else "stop_be",
                taker=True,
            )
            return
        if p.side < 0 and hi >= p.eff_stop:
            self._close(
                inst,
                ts,
                p.eff_stop * (1 + SLIP_BPS / 10_000.0),
                "stop" if not p.tp_filled else "stop_be",
                taker=True,
            )
            return

        # X2 分批 TP(严格穿越)
        if not p.tp_filled:
            if (p.side > 0 and hi > p.tp_px) or (p.side < 0 and lo < p.tp_px):
                self._close(
                    inst, ts, p.tp_px, "tp50", taker=False, fraction=TP_FRACTION
                )

        # MFE(R 单位)
        R = self.cfg.sl_atr * p.atr_entry
        if R > 0:
            fav = (cl - p.entry_px) * p.side
            p.mfe_r = max(p.mfe_r, fav / R)

        # X4 时间止损
        if p.bars_held >= MAX_HOLD_BARS and p.mfe_r < 1.0:
            self._close(inst, ts, cl, "time", taker=True)
            return

        # X5 regime 翻转出场
        regime_ok = self._regime(row, p.side)
        if not regime_ok:
            self._close(inst, ts, cl, "regime", taker=True)
            return

        # 吊灯更新(用当前 bar 的 ATR + close),供【下一根】盘中检查
        p.extreme_close = (
            max(p.extreme_close, cl) if p.side > 0 else min(p.extreme_close, cl)
        )
        atr_now = row.atr
        if not np.isnan(atr_now):
            if p.side > 0:
                trail = p.extreme_close - self.cfg.trail_atr * atr_now
                p.eff_stop = max(p.eff_stop, trail)
            else:
                trail = p.extreme_close + self.cfg.trail_atr * atr_now
                p.eff_stop = min(p.eff_stop, trail)

    # ---- regime / 入场 ----
    def _regime(self, row, side: int) -> bool:
        if np.isnan(row.cloud_top) or np.isnan(row.adx4):
            return False
        if row.adx4 <= self.cfg.adx_entry:
            return False
        return row.close4 > row.cloud_top if side > 0 else row.close4 < row.cloud_bot

    def _try_fill(self, inst: str, ts: dt.datetime, row) -> None:
        """E5:下根 bar 的 low < 限价(多)/ high > 限价(空)才算成交,严格不含 touch。"""
        lim = self.pending[inst]
        if lim is None or self.pos[inst] is not None:
            return
        side = self.pending_side[inst]
        filled = (row.low < lim) if side > 0 else (row.high > lim)
        if not filled:
            if (side > 0 and row.low == lim) or (side < 0 and row.high == lim):
                self.touch_rejects += 1
            return

        atr_e = self.pending_atr[inst]
        if atr_e <= 0 or np.isnan(atr_e):
            self.pending[inst] = None
            return

        qty = (RISK_PCT * self.equity) / (self.cfg.sl_atr * atr_e)
        notional = qty * lim
        # 缺失时间戳的资产不入字典,让 _gross_notional 回退到 entry_px(而非 0,
        # 否则会低估已有敞口、把 P2 上限架空)
        cur = self._cur
        prices = {i: float(cur[i].close) for i in INSTRUMENTS if i in cur}
        if self._gross_notional(prices) + notional > GROSS_CAP_MULT * self.equity:
            self.capacity_events += 1
            self.pending[inst] = None
            return

        self.n_fills += 1
        entry_cost = lim * qty * MAKER_BPS / 10_000.0
        self.equity -= entry_cost
        stop = lim - side * self.cfg.sl_atr * atr_e
        tp = lim + side * TP_R_MULT * self.cfg.sl_atr * atr_e
        tr = Trade(
            instrument=inst,
            side=side,
            entry_ts=ts,
            entry_px=lim,
            qty=qty,
            atr_entry=atr_e,
            maker_cost=entry_cost,
        )
        self.pos[inst] = Position(
            side=side,
            entry_px=lim,
            qty=qty,
            atr_entry=atr_e,
            entry_ts=ts,
            eff_stop=stop,
            tp_px=tp,
            extreme_close=lim,
            trade=tr,
        )
        self.pending[inst] = None

        # X6: 入场 bar 内若已触及止损 → 当 bar 止损(最坏假设)
        p = self.pos[inst]
        if (side > 0 and row.low <= stop) or (side < 0 and row.high >= stop):
            self._close(
                inst,
                ts,
                stop * (1 - side * SLIP_BPS / 10_000.0),
                "stop_entrybar",
                taker=True,
            )

    def _place(self, inst: str, ts: dt.datetime, row) -> None:
        """E1-E4:每根 1h 收盘 cancel-replace。"""
        if self.pos[inst] is not None or self._halted(ts):
            self.pending[inst] = None
            return
        if np.isnan(row.kijun) or np.isnan(row.atr) or np.isnan(row.cloud_top):
            self.pending[inst] = None
            return

        long_ok = self._regime(row, 1) and row.close > row.kijun
        short_ok = self._regime(row, -1) and row.close < row.kijun
        if long_ok:
            lim = row.kijun
            if lim < row.cloud_top:  # E3 地板:不在云里接
                self.pending[inst] = None
                return
            self.pending[inst], self.pending_side[inst] = lim, 1
            self.pending_atr[inst] = row.atr
            self.orders_placed += 1
        elif short_ok:
            lim = row.kijun
            if lim > row.cloud_bot:  # E3 镜像
                self.pending[inst] = None
                return
            self.pending[inst], self.pending_side[inst] = lim, -1
            self.pending_atr[inst] = row.atr
            self.orders_placed += 1
        else:
            self.pending[inst] = None  # E4

    def _funding(self, inst: str, ts: dt.datetime, row) -> None:
        """C5/F10:持仓逐小时结算。缺失期用保守代理,无论方向均为支付。"""
        p = self.pos[inst]
        if p is None:
            return
        notional = abs(p.qty) * row.close
        rate = row.funding
        if ts >= FUNDING_REAL_FROM and not pd.isna(rate):
            cost = notional * rate * p.side  # 正费率:多头付、空头收
        else:
            cost = notional * FUNDING_PROXY_PER_H  # 无论方向均支付
        p.trade.funding_cost += cost
        self.equity -= cost

    def run(self) -> dict:
        # 预物化成 itertuples 记录 + ts→序号索引:逐 bar 的 df.loc[ts] 在 17k×3×7
        # 规模下会拖到分钟级,这里只是取数加速,不改任何逻辑。
        recs = {i: list(df.itertuples()) for i, df in self.data.items()}
        idx = {i: {r.Index: k for k, r in enumerate(recs[i])} for i in self.data}
        timeline = sorted(set().union(*[set(m.keys()) for m in idx.values()]))

        for ts in timeline:
            self._roll_day(ts)
            self._cur = {
                i: recs[i][idx[i][ts]] for i in INSTRUMENTS if ts in idx[i]
            }  # 供 _try_fill 算组合敞口(P2)
            for inst in INSTRUMENTS:
                row = self._cur.get(inst)
                if row is None:
                    continue
                self._manage(inst, ts, row)  # 先管已有仓位(出场)
                self._try_fill(inst, ts, row)  # 再判上一根挂单是否成交
                self._funding(inst, ts, row)  # 持仓 funding
                self._place(inst, ts, row)  # 最后按本根收盘 cancel-replace
            self.equity_curve.append((ts, self.equity))
            self.gross_curve.append((ts, self.gross_equity))

        # 窗口末强平未平仓位(按收盘,taker)
        for inst in INSTRUMENTS:
            if self.pos[inst] is not None:
                d = self.data[inst]
                last_ts = d.index[-1]
                self._close(
                    inst, last_ts, float(d.loc[last_ts].close), "eow", taker=True
                )

        return self._summarize()

    def _summarize(self) -> dict:
        tr = self.trades
        gross = sum(t.gross_pnl for t in tr)
        maker = sum(t.maker_cost for t in tr)
        taker = sum(t.taker_cost for t in tr)
        fund = sum(t.funding_cost for t in tr)
        net = gross - maker - taker - fund
        return {
            "config": asdict(self.cfg),
            "n_trades": len(tr),
            "gross_pnl": gross,
            "maker_cost": maker,
            "taker_cost": taker,
            "funding_cost": fund,
            "net_pnl": net,
            "final_equity": self.equity,
            "capacity_events": self.capacity_events,
            "orders_placed": self.orders_placed,
            "n_fills": self.n_fills,
            "gross_curve": self.gross_curve,
            "touch_rejects": self.touch_rejects,
            "trades": tr,
            "equity_curve": self.equity_curve,
        }


def trade_returns(trades: list[Trade], mode: str = "net") -> np.ndarray:
    """每笔交易的收益率(相对入场名义),供 DSR/Sharpe。"""
    out = []
    for t in trades:
        notional = t.entry_px * t.qty
        if notional <= 0:
            continue
        p = t.gross_pnl if mode == "gross" else t.net_pnl
        out.append(p / notional)
    return np.asarray(out, dtype=float)


async def run_config(
    cfg: TPBConfig, data: dict[str, pd.DataFrame] | None = None
) -> dict:
    if data is None:
        data = await build_dataset(cfg)
    return PortfolioSim(cfg, data).run()


async def _main(once: bool) -> None:
    data = await build_dataset(TPBConfig())
    for inst, d in data.items():
        print(
            f"{inst}: 1h bars={len(d)} {d.index[0]:%Y-%m-%d} → {d.index[-1]:%Y-%m-%d}"
        )
    r = await run_config(TPBConfig(), data)
    print(
        f"\n中心配置: n_trades={r['n_trades']} gross={r['gross_pnl']:.2f} "
        f"net={r['net_pnl']:.2f} equity={r['final_equity']:.2f}"
    )


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    a = ap.parse_args()
    asyncio.run(_main(a.once))
