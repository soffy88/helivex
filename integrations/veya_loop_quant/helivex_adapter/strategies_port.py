"""现有策略的引擎侧移植（研究态）— 只实现信号，不重写引擎。

把 `strategies/*.yaml`（paper 层 Nautilus 策略）的信号逻辑翻译成
`helios_backtest_v51.StrategyInterface`（fingerprint/reset/on_bar/config_space），
复用引擎的撮合 / 成本 / CPCV / DSR 真相。差异与简化（诚实声明）：
- 引擎逐 bar 收盘撮合（bar-close 假设），与 R4.x gate 口径一致；
- sizing 统一 ±0.5 固定分数（研究对比用；成本相对影响与仓位无关）；
- 未计 funding（DB funding 历史仅 2026-06-30 起；<1bp/回合量级）；
- 每个策略只移植 YAML 文档化的核心信号，非 paper 层全部执行细节。

参数名与 YAML 对应；config_space() 声明搜索广度（DSR 通缩 n_trials 口径）。
"""
from __future__ import annotations

import hashlib
import json
import math
from collections import deque
from dataclasses import dataclass, field, fields
from typing import Any, Dict, Optional, Sequence

from helios_backtest_v51.interface import BarRow, TradeIntent

# 仓位目标：研究对比统一半仓（±0.5）
POSITION = 1.0  # 信号方向单位分数；实际仓位由 position_fraction 缩放（默认 0.5）


# ---------------------------------------------------------------------------
# 指标原语（纯函数 / deque 累积）
# ---------------------------------------------------------------------------

def ema(prev: float, x: float, period: int) -> float:
    k = 2.0 / (period + 1.0)
    return prev + k * (x - prev)


def wilder_atr(trs: Sequence[float], period: int) -> float:
    return sum(trs[-period:]) / period


def true_range(h: float, l: float, prev_close: Optional[float]) -> float:
    if prev_close is None:
        return h - l
    return max(h - l, abs(h - prev_close), abs(l - prev_close))


def rsi(closes: Sequence[float], period: int) -> float:
    """Wilder RSI，归一化到 0..1（YAML 用 0.3/0.7 口径）。"""
    if len(closes) < period + 1:
        return 0.5
    gains = losses = 0.0
    for i in range(-period, 0):
        d = closes[i] - closes[i - 1]
        gains += max(d, 0.0)
        losses += max(-d, 0.0)
    if losses == 0.0:
        return 1.0
    rs = gains / losses
    return rs / (1.0 + rs)


def adx(highs: Sequence[float], lows: Sequence[float], closes: Sequence[float],
        period: int) -> float:
    """Wilder ADX。需 period*2+1 根历史。"""
    n = len(closes)
    need = 2 * period + 1
    if n < need:
        return 0.0
    dm_p = dm_m = 0.0
    tr = 0.0
    for i in range(-period, 0):
        up = highs[i] - highs[i - 1]
        dn = lows[i - 1] - lows[i]
        dm_p += up if (up > dn and up > 0) else 0.0
        dm_m += dn if (dn > up and dn > 0) else 0.0
        tr += true_range(highs[i], lows[i], closes[i - 1])
    pdi = 100.0 * dm_p / max(tr, 1e-12)
    mdi = 100.0 * dm_m / max(tr, 1e-12)
    dx = 100.0 * abs(pdi - mdi) / max(pdi + mdi, 1e-12)
    return dx


def supertrend(highs: Sequence[float], lows: Sequence[float], closes: Sequence[float],
               period: int, mult: float, prev_dir: int, prev_st: float) -> tuple[float, int]:
    """返回 (st_line, direction)，direction=+1 多头 / -1 空头。"""
    n = len(closes)
    if n < period + 1:
        return prev_st, prev_dir
    atr = wilder_atr(
        [true_range(highs[i], lows[i], closes[i - 1]) for i in range(n - period, n)],
        period,
    )
    hl2 = (highs[-1] + lows[-1]) / 2.0
    upper = hl2 + mult * atr
    lower = hl2 - mult * atr
    if closes[-1] > prev_st if prev_dir == 1 else closes[-1] < prev_st:
        pass
    if closes[-1] <= upper:
        upper = min(upper, prev_st) if prev_dir == 1 else upper
    if closes[-1] >= lower:
        lower = max(lower, prev_st) if prev_dir == -1 else lower
    if closes[-1] > upper:
        return lower, +1
    if closes[-1] < lower:
        return upper, -1
    return (lower if prev_dir == 1 else upper), prev_dir


def macd_hist(closes: Sequence[float], fast: int, slow: int, signal: int) -> float:
    n = len(closes)
    if n < slow + signal:
        return 0.0
    ema_fast = sum(closes[-fast:]) / fast
    ema_slow = sum(closes[-slow:]) / slow
    # 粗近似：线性权重 EMA 末端值；信号线用差分窗口
    for i in range(-min(fast, slow) + 1, 0):
        ema_fast = ema(ema_fast, closes[i], fast)
        ema_slow = ema(ema_slow, closes[i], slow)
    macd_line = ema_fast - ema_slow
    hist = macd_line - sum(macd_line for _ in range(signal)) / signal if signal else 0.0
    return hist


def stoch_k(closes: Sequence[float], highs: Sequence[float], lows: Sequence[float],
            k_period: int) -> float:
    n = len(closes)
    if n < k_period:
        return 0.5
    hh = max(highs[-k_period:])
    ll = min(lows[-k_period:])
    return (closes[-1] - ll) / max(hh - ll, 1e-12)


def rolling_vwap_z(closes: Sequence[float], volumes: Sequence[float],
                   window: int, std_window: Optional[int] = None) -> float:
    """z = (close − VWAP_prior_n) / stdev(prior closes)。

    与 paper/strategies/{scalp_5m,vwap_mr_1h}.py 口径一致：VWAP 用前 window 根
    （shift(1)），σ 用前 std_window 根收盘价样本标准差（ddof=1）；
    std_window=None 时与 window 同窗。
    """
    n = len(closes)
    sw = std_window or window
    if n < max(window, sw) + 1:
        return 0.0
    prior_c = closes[-(window + 1):-1]
    prior_v = volumes[-(window + 1):-1]
    vwap = sum(c * vol for c, vol in zip(prior_c, prior_v)) / max(sum(prior_v), 1e-12)
    prior_std_c = closes[-(sw + 1):-1]
    if len(prior_std_c) < 2:
        return 0.0
    mean = sum(prior_std_c) / len(prior_std_c)
    sd = (sum((x - mean) ** 2 for x in prior_std_c) / (len(prior_std_c) - 1)) ** 0.5
    return (closes[-1] - vwap) / max(sd, 1e-10)


# ---------------------------------------------------------------------------
# 基类：状态管理 / fingerprint / config_space
# ---------------------------------------------------------------------------

class BasePortStrategy:
    """通用骨架：params dict → 指标状态 → _signal() → TradeIntent。"""

    name = "base"
    timeframe = "1h"
    # (参数名, 默认值) — 子类覆写；int_fields 标记整数参数
    params: Dict[str, Any] = {}
    int_fields: frozenset = frozenset()
    config_grid: Dict[str, Sequence[float]] = {}
    # 需要保留的 OHLC 历史（信号 warmup）
    min_history = 60

    def __init__(self, params: Optional[Dict[str, float]] = None,
                 position_fraction: float = 0.5) -> None:
        """position_fraction：目标仓位（信号方向 × 该比例，默认半仓）。"""
        merged = dict(self.params)
        if params:
            unknown = set(params) - set(merged)
            if unknown:
                raise ValueError(f"{self.name}: unknown params {sorted(unknown)}")
            for k, v in params.items():
                merged[k] = int(round(v)) if k in self.int_fields else float(v)
        self.p = merged
        self._position_fraction = float(position_fraction)
        self._closes: deque = deque()
        self._highs: deque = deque()
        self._lows: deque = deque()
        self._vols: deque = deque()
        self._hold = 0          # 持仓剩余 bar
        self._entry_dir = 0
        self._trailing_stop = None
        self._st_dir = 1
        self._st_line = 0.0
        self._ema_fast = 0.0
        self._ema_slow = 0.0
        self._fingerprint = self._make_fingerprint()

    # ── StrategyInterface ────────────────────────────────────────────────
    def fingerprint(self) -> object:
        return self._fingerprint

    def reset(self, initial_state=None) -> None:
        self._closes.clear(); self._highs.clear(); self._lows.clear(); self._vols.clear()
        self._hold = 0; self._entry_dir = 0; self._trailing_stop = None
        self._st_dir = 1; self._st_line = 0.0
        self._ema_fast = 0.0; self._ema_slow = 0.0

    def config_space(self) -> dict:
        return self.config_grid

    def on_bar(self, bar: BarRow) -> TradeIntent:
        self._feed(bar)
        if len(self._closes) < self.min_history:
            return TradeIntent(bar.bar_close_ts, 0.0)
        target = self._position_fraction * self._signal(bar)
        return TradeIntent(bar.bar_close_ts, max(-1.0, min(1.0, target)))

    # ── 子类接口 ─────────────────────────────────────────────────────────
    def _feed(self, bar: BarRow) -> None:
        self._closes.append(bar.close)
        self._highs.append(bar.high)
        self._lows.append(bar.low)
        self._vols.append(float(bar.features.get("volume", 0.0) or 0.0))

    def _signal(self, bar: BarRow) -> float:
        raise NotImplementedError

    # ── 工具 ─────────────────────────────────────────────────────────────
    def _make_fingerprint(self) -> str:
        payload = {"name": self.name, "params": self.p}
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True).encode()
        ).hexdigest()[:16]

    def _clamp(self, x: float) -> float:
        return max(-1.0, min(1.0, x))


# ---------------------------------------------------------------------------
# 1. trend_dual — 4H SuperTrend+EMA+ADX+MACD confluence（YAML: strategies/trend_dual.yaml）
# ---------------------------------------------------------------------------

class TrendDual(BasePortStrategy):
    name = "trend_dual"
    timeframe = "4h"
    params = dict(st_period=10, st_mult=3.0, ema_fast=20, ema_slow=50,
                  adx_period=14, adx_threshold=25.0, macd_fast=12, macd_slow=26,
                  macd_signal=9, min_confluence=2)
    int_fields = frozenset({"st_period", "ema_fast", "ema_slow", "adx_period",
                            "macd_fast", "macd_slow", "macd_signal", "min_confluence"})
    config_grid = {"st_mult": [2.0, 3.0, 4.0], "ema_fast": [10, 20, 30],
                   "adx_threshold": [20.0, 25.0, 30.0]}
    min_history = 80

    def _feed(self, bar):
        super()._feed(bar)
        p = self.p
        self._st_line, self._st_dir = supertrend(
            list(self._highs), list(self._lows), list(self._closes),
            p["st_period"], p["st_mult"], self._st_dir, self._st_line)
        if len(self._closes) == 1:
            self._ema_fast = self._ema_slow = bar.close
        self._ema_fast = ema(self._ema_fast, bar.close, p["ema_fast"])
        self._ema_slow = ema(self._ema_slow, bar.close, p["ema_slow"])

    def _signal(self, bar) -> float:
        p = self.p
        c, h, l = list(self._closes), list(self._highs), list(self._lows)
        st_bull = c[-1] > self._st_line
        ema_bull = self._ema_fast > self._ema_slow
        adx_ok = adx(h, l, c, p["adx_period"]) >= p["adx_threshold"]
        macd_ok = macd_hist(c, p["macd_fast"], p["macd_slow"], p["macd_signal"]) > 0.0
        if st_bull:
            score = sum([ema_bull, adx_ok, macd_ok])
            return 1.0 if score + 1 >= p["min_confluence"] else 0.0
        score = sum([not ema_bull, adx_ok, not macd_ok])
        return -1.0 if score + 1 >= p["min_confluence"] else 0.0


# ---------------------------------------------------------------------------
# 2. trend_follower_port — 1D Donchian20 + ADX 门 + Chandelier ATR×3（YAML）
# ---------------------------------------------------------------------------

class TrendFollowerPort(BasePortStrategy):
    name = "trend_follower_port"
    timeframe = "1d"
    params = dict(donchian_n=20, adx_period=14, adx_entry=20.0, use_ema=True,
                  ema_period=50, chandelier_period=22, chandelier_mult=3.0,
                  max_holding_days=30)
    int_fields = frozenset({"donchian_n", "adx_period", "ema_period",
                            "chandelier_period", "max_holding_days"})
    config_grid = {"donchian_n": [15, 20, 30], "chandelier_mult": [2.0, 3.0, 4.0],
                   "adx_entry": [15.0, 20.0, 25.0]}
    min_history = 120

    def _signal(self, bar) -> float:
        p = self.p
        c, h, l = list(self._closes), list(self._highs), list(self._lows)
        n = p["donchian_n"]
        if self._entry_dir == 0:
            if len(c) < n + p["adx_period"] * 2 + 2:
                return 0.0
            if adx(h, l, c, p["adx_period"]) < p["adx_entry"]:
                return 0.0
            ema50 = sum(c[-p["ema_period"]:]) / p["ema_period"] if p["use_ema"] else 0.0
            if c[-1] > max(h[-n - 1:-1]) and (not p["use_ema"] or c[-1] > ema50):
                self._entry_dir = 1
                self._hold = p["max_holding_days"]
                self._trailing_stop = max(h[-p["chandelier_period"]:]) - \
                    p["chandelier_mult"] * wilder_atr(
                        [true_range(h[i], l[i], c[i - 1]) for i in range(-p["chandelier_period"], 0)],
                        p["chandelier_period"])
                return 1.0
            if c[-1] < min(l[-n - 1:-1]) and (not p["use_ema"] or c[-1] < ema50):
                self._entry_dir = -1
                self._hold = p["max_holding_days"]
                self._trailing_stop = min(l[-p["chandelier_period"]:]) + \
                    p["chandelier_mult"] * wilder_atr(
                        [true_range(h[i], l[i], c[i - 1]) for i in range(-p["chandelier_period"], 0)],
                        p["chandelier_period"])
                return -1.0
            return 0.0
        # 持仓：Chandelier 移动止损 / 时间止损
        self._hold -= 1
        if self._entry_dir == 1:
            self._trailing_stop = max(self._trailing_stop,
                                      max(h[-p["chandelier_period"]:]) -
                                      p["chandelier_mult"] * wilder_atr(
                                          [true_range(h[i], l[i], c[i - 1]) for i in range(-p["chandelier_period"], 0)],
                                          p["chandelier_period"]))
            if c[-1] <= self._trailing_stop or self._hold <= 0:
                self._entry_dir = 0
                return 0.0
            return 1.0
        self._trailing_stop = min(self._trailing_stop,
                                  min(l[-p["chandelier_period"]:]) +
                                  p["chandelier_mult"] * wilder_atr(
                                      [true_range(h[i], l[i], c[i - 1]) for i in range(-p["chandelier_period"], 0)],
                                      p["chandelier_period"]))
        if c[-1] >= self._trailing_stop or self._hold <= 0:
            self._entry_dir = 0
            return 0.0
        return -1.0


# ---------------------------------------------------------------------------
# 3. vwap_mr_1h — 1H VWAP z-score 均值回归（忠实 paper/strategies/vwap_mr_1h.py）
#    paper 实现比 YAML 简单：入场只有纯 z-score（无 BB/RSI/Stoch confluence），
#    出场 = SL/TP 括号（sl_std×σ / min_rr×SL）优先 + hold 时间出场 + 止损冷却。
# ---------------------------------------------------------------------------

class VwapMr1H(BasePortStrategy):
    name = "vwap_mr_1h"
    timeframe = "1h"
    params = dict(vwap_n=4, std_n=24, z_thr=2.0, hold=6,
                  sl_std=1.0, min_rr=1.5, cooldown_after_sl=0)
    int_fields = frozenset({"vwap_n", "std_n", "hold", "cooldown_after_sl"})
    config_grid = {"z_thr": [1.5, 2.0, 3.0], "std_n": [12, 24, 48],
                   "hold": [4, 6, 8]}
    min_history = 40

    def reset(self, initial_state=None) -> None:
        super().reset(initial_state)
        self._sl_cooldown = 0
        self._entry_px = None
        self._sl_dist = None

    def _signal(self, bar) -> float:
        p = self.p
        c, v = list(self._closes), list(self._vols)
        if self._entry_dir != 0:
            edge = (c[-1] - self._entry_px) * self._entry_dir
            if self._entry_px is not None and self._sl_dist:
                if edge <= -self._sl_dist:
                    self._sl_cooldown = p["cooldown_after_sl"]
                    self._exit()
                    return 0.0
                if edge >= p["min_rr"] * self._sl_dist:
                    self._exit()
                    return 0.0
            self._hold -= 1
            if self._hold <= 0:
                self._exit()
                return 0.0
            return float(self._entry_dir)
        if self._sl_cooldown > 0:
            self._sl_cooldown -= 1
            return 0.0
        z = rolling_vwap_z(c, v, p["vwap_n"], std_window=p["std_n"])
        std = _rolling_std(c, p["std_n"])
        if z >= p["z_thr"]:
            self._enter(-1, std)
            return -1.0
        if z <= -p["z_thr"]:
            self._enter(1, std)
            return 1.0
        return 0.0

    def _enter(self, direction: int, std: float) -> None:
        p = self.p
        self._entry_dir = direction
        self._entry_px = self._closes[-1]
        self._sl_dist = p["sl_std"] * std
        self._hold = p["hold"]

    def _exit(self) -> None:
        self._entry_dir = 0
        self._entry_px = None
        self._sl_dist = None


def _rolling_std(closes: Sequence[float], window: int) -> float:
    """前 window 根收盘价样本标准差（不含当前 bar，与 paper 一致）。"""
    n = len(closes)
    if n < window + 1:
        return 0.0
    seg = closes[-(window + 1):-1]
    mean = sum(seg) / len(seg)
    return (sum((x - mean) ** 2 for x in seg) / (len(seg) - 1)) ** 0.5


# ---------------------------------------------------------------------------
# 4. scalper_v2_port — 5M ADX 滞回 + RSI/BB 双模式 + SL/TP 括号（YAML + paper）
#    忠实 paper/strategies/scalper_v2_port.py:_evaluate（2026-07 版）：
#    - ADX 滞回状态机：mr→bo（adx≥enter）/ bo→mr（adx<exit）+ cooldown
#    - MR 入场：仅 RSI 极端（≤oversold / ≥overbought），ATR 健康过滤
#    - MR 出场：SL/TP 括号（SL=sl_atr_mult×ATR，TP=min_rr×SL）；无锚点回退
#      BB 中轨 / RSI 中性出场
#    - BO 入场：close 破 BB ± EMA50/MACD 趋势一致过滤
#    - BO 出场：ATR 追踪止损 或 ADX<breakout_exit_adx
#    - max_holding_bars 时间出场；cooldown_after_sl 止损冷却
#    引擎差异（注明）：SL/TP 为 bar-close 检查而非触价单（更保守）。
# ---------------------------------------------------------------------------

def _ema_series_value(closes: Sequence[float], period: int) -> float:
    """滚动 EMA 的末端值（从窗口均值起步）。"""
    if not closes:
        return 0.0
    start = max(1, len(closes) - 4 * period)
    v = sum(closes[start:]) / max(len(closes) - start, 1)
    for x in closes[start + 1:]:
        v = ema(v, x, period)
    return v


def _macd_hist_value(closes: Sequence[float], fast: int, slow: int, signal: int) -> float:
    """MACD 直方图末端值（滚动 EMA12/26 → EMA9 of macd_line）。"""
    n = len(closes)
    need = slow + signal
    if n < need:
        return 0.0
    seg = closes[n - need:]
    macd_line = 0.0
    ef = sum(seg[:fast]) / fast
    es = sum(seg[:slow]) / slow
    mline_hist: list[float] = []
    for i, x in enumerate(seg):
        ef = ema(ef, x, fast)
        es = ema(es, x, slow)
        macd_line = ef - es
        if i >= slow - 1:
            mline_hist.append(macd_line)
    sig = sum(mline_hist[:signal]) / signal
    for v in mline_hist[signal:]:
        sig = ema(sig, v, signal)
    return macd_line - sig


class ScalperV2Port(BasePortStrategy):
    name = "scalper_v2_port"
    timeframe = "5m"
    params = dict(bb_period=20, bb_k=2.0, rsi_period=14,
                  rsi_oversold=30.0, rsi_overbought=70.0,
                  adx_period=14, adx_enter_breakout=22.0, adx_exit_breakout=18.0,
                  cooldown_bars=4, rsi_neutral_low=45.0, rsi_neutral_high=55.0,
                  trailing_atr_mult=1.5, breakout_exit_adx=15.0,
                  max_holding_bars=48, atr_health_min=0.0005, atr_health_max=0.05,
                  sl_atr_mult=1.0, min_rr=1.5, cooldown_after_sl=0,
                  use_ema=True, ema_period=50,
                  use_macd=True, macd_fast=12, macd_slow=26, macd_signal=9)
    int_fields = frozenset({"bb_period", "rsi_period", "adx_period", "cooldown_bars",
                            "max_holding_bars", "ema_period", "macd_fast",
                            "macd_slow", "macd_signal", "cooldown_after_sl"})
    config_grid = {"adx_enter_breakout": [18.0, 22.0, 26.0], "bb_k": [1.5, 2.0, 2.5],
                   "trailing_atr_mult": [1.2, 1.5, 2.0]}
    min_history = 70

    def reset(self, initial_state=None) -> None:
        super().reset(initial_state)
        self._mode = "mr"
        self._cooldown = 0
        self._entry_mode = "mr"
        self._entry_px = None
        self._sl_dist = None
        self._trail_extreme = None
        self._bars_held = 0
        self._sl_cooldown = 0

    def _feed(self, bar):
        super()._feed(bar)  # 仅累积 OHLCV，指标在 _signal 内现算（与 paper 一致）

    def _signal(self, bar) -> float:
        p = self.p
        c, h, l = list(self._closes), list(self._highs), list(self._lows)
        need = max(p["bb_period"], p["rsi_period"], 2 * p["adx_period"],
                   p["ema_period"], p["macd_slow"] + p["macd_signal"]) + 1
        if len(c) < need:
            return 0.0
        adx_v = adx(h, l, c, p["adx_period"])
        atr_v = wilder_atr([true_range(h[i], l[i], c[i - 1]) for i in range(-p["adx_period"], 0)],
                           p["adx_period"])
        r = rsi(c, p["rsi_period"]) * 100.0
        mid = sum(c[-p["bb_period"]:]) / p["bb_period"]
        sd = (sum((x - mid) ** 2 for x in c[-p["bb_period"]:]) / p["bb_period"]) ** 0.5
        upper, lower = mid + p["bb_k"] * sd, mid - p["bb_k"] * sd
        atr_pct = atr_v / c[-1] if c[-1] else 0.0
        health_ok = p["atr_health_min"] <= atr_pct <= p["atr_health_max"]
        ema_v = _ema_series_value(c, p["ema_period"]) if p["use_ema"] else None
        macd_h = _macd_hist_value(c, p["macd_fast"], p["macd_slow"], p["macd_signal"]) \
            if p["use_macd"] else None
        long_conf = ((not p["use_ema"]) or ema_v is None or c[-1] > ema_v) and \
            ((not p["use_macd"]) or macd_h is None or macd_h >= 0)
        short_conf = ((not p["use_ema"]) or ema_v is None or c[-1] < ema_v) and \
            ((not p["use_macd"]) or macd_h is None or macd_h <= 0)

        # ADX 滞回模式切换 + 冷却
        if self._sl_cooldown > 0 and self._entry_dir == 0:
            self._sl_cooldown -= 1
        elif self._cooldown > 0:
            self._cooldown -= 1
        else:
            if self._mode == "mr" and adx_v >= p["adx_enter_breakout"]:
                self._mode, self._cooldown = "bo", p["cooldown_bars"]
            elif self._mode == "bo" and adx_v < p["adx_exit_breakout"]:
                self._mode, self._cooldown = "mr", p["cooldown_bars"]

        if self._entry_dir == 0:
            if self._sl_cooldown > 0:
                return 0.0
            if not health_ok:
                return 0.0
            if self._mode == "mr":
                if r <= p["rsi_oversold"]:
                    self._enter(1, atr_v, c[-1])
                    return 1.0
                if r >= p["rsi_overbought"]:
                    self._enter(-1, atr_v, c[-1])
                    return -1.0
            else:  # breakout：趋势一致过滤
                if c[-1] > upper and long_conf:
                    self._enter(1, atr_v, c[-1])
                    return 1.0
                if c[-1] < lower and short_conf:
                    self._enter(-1, atr_v, c[-1])
                    return -1.0
            return 0.0

        # ── 持仓管理 ──
        self._bars_held += 1
        action = None
        if self._entry_dir == 1:
            if self._entry_mode == "mr":
                if self._entry_px is not None and self._sl_dist:
                    if c[-1] <= self._entry_px - self._sl_dist:
                        action = "exit"
                        self._sl_cooldown = p["cooldown_after_sl"]
                    elif c[-1] >= self._entry_px + p["min_rr"] * self._sl_dist:
                        action = "exit"
                elif c[-1] >= mid or r >= p["rsi_neutral_low"]:
                    action = "exit"
            else:
                self._trail_extreme = max(self._trail_extreme or c[-1], c[-1])
                if (c[-1] < self._trail_extreme - atr_v * p["trailing_atr_mult"]
                        or adx_v < p["breakout_exit_adx"]):
                    action = "exit"
            if action is None and self._bars_held >= p["max_holding_bars"]:
                action = "exit"
        else:
            if self._entry_mode == "mr":
                if self._entry_px is not None and self._sl_dist:
                    if c[-1] >= self._entry_px + self._sl_dist:
                        action = "exit"
                        self._sl_cooldown = p["cooldown_after_sl"]
                    elif c[-1] <= self._entry_px - p["min_rr"] * self._sl_dist:
                        action = "exit"
                elif c[-1] <= mid or r <= p["rsi_neutral_high"]:
                    action = "exit"
            else:
                self._trail_extreme = min(self._trail_extreme or c[-1], c[-1])
                if (c[-1] > self._trail_extreme + atr_v * p["trailing_atr_mult"]
                        or adx_v < p["breakout_exit_adx"]):
                    action = "exit"
            if action is None and self._bars_held >= p["max_holding_bars"]:
                action = "exit"
        if action:
            self._entry_dir = 0
            self._entry_px = None
            self._sl_dist = None
            self._trail_extreme = None
            self._bars_held = 0
            return 0.0
        return float(self._entry_dir)

    def _enter(self, direction: int, atr_v: float, close: float) -> None:
        p = self.p
        self._entry_dir = direction
        self._entry_mode = self._mode
        self._bars_held = 0
        self._entry_px = close
        self._sl_dist = atr_v * (p["sl_atr_mult"] if self._mode == "mr" else p["trailing_atr_mult"])
        self._trail_extreme = close


# ---------------------------------------------------------------------------
# 5. futures_signal_port — 1H 突破 + 量增 + RSI 区间 + EMA/MACD 确认 + SL/TP 括号
#    忠实 paper/strategies/futures_signal_port.py（helixa futures-signal-engine 移植）：
#    - 入场：收盘破前 n 根通道 + vol≥surge×volMA + RSI 区间 + long_conf/short_conf
#      （EMA50 方向 + MACD 直方图符号）
#    - 出场：SL/TP 括号（sl_atr_mult×ATR / min_rr×SL）优先；退回对称出场
#      （反向通道突破 或 RSI 离开区间）
# ---------------------------------------------------------------------------

class FuturesSignalPort(BasePortStrategy):
    name = "futures_signal_port"
    timeframe = "1h"
    params = dict(breakout_period=20, vol_ma_period=20, vol_surge_mult=1.5,
                  rsi_period=14, rsi_long_min=45.0, rsi_long_max=75.0,
                  rsi_short_min=25.0, rsi_short_max=55.0,
                  sl_atr_mult=2.0, min_rr=1.5,
                  use_ema=True, ema_period=50,
                  use_macd=True, macd_fast=12, macd_slow=26, macd_signal=9)
    int_fields = frozenset({"breakout_period", "vol_ma_period", "rsi_period",
                            "ema_period", "macd_fast", "macd_slow", "macd_signal"})
    config_grid = {"breakout_period": [15, 20, 30], "vol_surge_mult": [1.2, 1.5, 2.0],
                   "rsi_period": [10, 14, 20]}
    min_history = 70

    def reset(self, initial_state=None) -> None:
        super().reset(initial_state)
        self._entry_px = None
        self._sl_dist = None

    def _signal(self, bar) -> float:
        p = self.p
        c, h, l = list(self._closes), list(self._highs), list(self._lows)
        v = list(self._vols)
        n = p["breakout_period"]
        if len(c) < n + p["vol_ma_period"] + p["rsi_period"]:
            return 0.0
        high_bo = max(h[-n - 1:-1])
        low_bo = min(l[-n - 1:-1])
        vol_ma = sum(v[-p["vol_ma_period"]:]) / p["vol_ma_period"]
        surge = v[-1] >= p["vol_surge_mult"] * vol_ma
        r = rsi(c, p["rsi_period"]) * 100.0
        ema_v = _ema_series_value(c, p["ema_period"]) if p["use_ema"] else None
        macd_h = _macd_hist_value(c, p["macd_fast"], p["macd_slow"], p["macd_signal"]) \
            if p["use_macd"] else None
        long_conf = ((not p["use_ema"]) or ema_v is None or c[-1] > ema_v) and \
            ((not p["use_macd"]) or macd_h is None or macd_h >= 0)
        short_conf = ((not p["use_ema"]) or ema_v is None or c[-1] < ema_v) and \
            ((not p["use_macd"]) or macd_h is None or macd_h <= 0)
        atr_v = wilder_atr([true_range(h[i], l[i], c[i - 1]) for i in range(-p["rsi_period"], 0)],
                           p["rsi_period"])

        if self._entry_dir == 0:
            if (c[-1] > high_bo and surge and p["rsi_long_min"] <= r <= p["rsi_long_max"]
                    and long_conf):
                self._enter(1, atr_v, c[-1])
                return 1.0
            if (c[-1] < low_bo and surge and p["rsi_short_min"] <= r <= p["rsi_short_max"]
                    and short_conf):
                self._enter(-1, atr_v, c[-1])
                return -1.0
            return 0.0
        # 持仓：SL/TP 括号优先，退回对称出场
        action = None
        if self._entry_px is not None and self._sl_dist:
            if self._entry_dir == 1:
                if c[-1] <= self._entry_px - self._sl_dist:
                    action = "exit"
                elif c[-1] >= self._entry_px + p["min_rr"] * self._sl_dist:
                    action = "exit"
            else:
                if c[-1] >= self._entry_px + self._sl_dist:
                    action = "exit"
                elif c[-1] <= self._entry_px - p["min_rr"] * self._sl_dist:
                    action = "exit"
        if action is None:
            if self._entry_dir == 1 and (c[-1] < low_bo or r > p["rsi_long_max"]):
                action = "exit"
            if self._entry_dir == -1 and (c[-1] > high_bo or r < p["rsi_short_min"]):
                action = "exit"
        if action:
            self._exit()
            return 0.0
        return float(self._entry_dir)

    def _enter(self, direction: int, atr_v: float, close: float) -> None:
        p = self.p
        self._entry_dir = direction
        self._entry_px = close
        self._sl_dist = p["sl_atr_mult"] * atr_v

    def _exit(self) -> None:
        self._entry_dir = 0
        self._entry_px = None
        self._sl_dist = None


# ---------------------------------------------------------------------------
# 6. scalp_5m — 5M VWAP-MR z 阈值 + 时间出场（YAML）
# ---------------------------------------------------------------------------

class Scalp5M(BasePortStrategy):
    name = "scalp_5m"
    timeframe = "5m"
    params = dict(vwap_window=12, z_threshold=2.0, hold_bars=6)
    int_fields = frozenset({"vwap_window", "hold_bars"})
    config_grid = {"vwap_window": [8, 12, 20], "z_threshold": [1.5, 2.0, 3.0],
                   "hold_bars": [4, 6, 10]}
    min_history = 40

    def _signal(self, bar) -> float:
        p = self.p
        c, v = list(self._closes), list(self._vols)
        if self._entry_dir != 0:
            self._hold -= 1
            if self._hold <= 0:
                self._entry_dir = 0
                return 0.0
            return float(self._entry_dir)
        z = rolling_vwap_z(c, v, p["vwap_window"])
        if z <= -p["z_threshold"]:
            self._entry_dir = 1
            self._hold = p["hold_bars"]
            return 1.0
        if z >= p["z_threshold"]:
            self._entry_dir = -1
            self._hold = p["hold_bars"]
            return -1.0
        return 0.0


# ---------------------------------------------------------------------------
# 注册表
# ---------------------------------------------------------------------------

STRATEGY_REGISTRY: Dict[str, type] = {
    cls.name: cls for cls in (
        TrendDual, TrendFollowerPort, VwapMr1H, ScalperV2Port,
        FuturesSignalPort, Scalp5M,
    )
}

__all__ = ["STRATEGY_REGISTRY"] + list(STRATEGY_REGISTRY)
