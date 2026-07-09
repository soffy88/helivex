"""Shared hand-rolled indicators for the ported helixa strategies.

Deterministic pure functions over OHLC lists (Wilder smoothing where applicable),
so the live paper node has no dependency on a specific NautilusTrader indicator API
version. Unit-tested offline (see tests). Used by trend_follower_port + scalper_v2_port.
"""

from __future__ import annotations


def wilder_atr(
    highs: list[float], lows: list[float], closes: list[float], period: int
) -> float | None:
    """Wilder ATR over the last `period` true ranges. Needs period+1 bars."""
    n = len(closes)
    if n < period + 1:
        return None
    trs: list[float] = []
    for i in range(1, n):
        trs.append(
            max(
                highs[i] - lows[i],
                abs(highs[i] - closes[i - 1]),
                abs(lows[i] - closes[i - 1]),
            )
        )
    atr = sum(trs[:period]) / period
    for tr in trs[period:]:
        atr = (atr * (period - 1) + tr) / period
    return atr


def wilder_adx(
    highs: list[float], lows: list[float], closes: list[float], period: int
) -> float | None:
    """Wilder ADX. Needs ~2*period+1 bars to be meaningful."""
    n = len(closes)
    if n < 2 * period + 1:
        return None
    plus_dm: list[float] = []
    minus_dm: list[float] = []
    trs: list[float] = []
    for i in range(1, n):
        up = highs[i] - highs[i - 1]
        dn = lows[i - 1] - lows[i]
        plus_dm.append(up if (up > dn and up > 0) else 0.0)
        minus_dm.append(dn if (dn > up and dn > 0) else 0.0)
        trs.append(
            max(
                highs[i] - lows[i],
                abs(highs[i] - closes[i - 1]),
                abs(lows[i] - closes[i - 1]),
            )
        )

    def _wilder(seq: list[float]) -> list[float]:
        out: list[float] = []
        s = sum(seq[:period])
        out.append(s)
        for v in seq[period:]:
            s = s - (s / period) + v
            out.append(s)
        return out

    atr_s = _wilder(trs)
    pdm_s = _wilder(plus_dm)
    mdm_s = _wilder(minus_dm)
    dxs: list[float] = []
    for a, p, m in zip(atr_s, pdm_s, mdm_s):
        if a == 0:
            continue
        pdi = 100.0 * p / a
        mdi = 100.0 * m / a
        denom = pdi + mdi
        if denom == 0:
            continue
        dxs.append(100.0 * abs(pdi - mdi) / denom)
    if len(dxs) < period:
        return None
    adx = sum(dxs[:period]) / period
    for dx in dxs[period:]:
        adx = (adx * (period - 1) + dx) / period
    return adx


def wilder_rsi(closes: list[float], period: int) -> float | None:
    """Wilder RSI in [0, 100]. Needs period+1 closes."""
    if len(closes) < period + 1:
        return None
    gains: list[float] = []
    losses: list[float] = []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i - 1]
        gains.append(max(d, 0.0))
        losses.append(max(-d, 0.0))
    avg_g = sum(gains[:period]) / period
    avg_l = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_g = (avg_g * (period - 1) + gains[i]) / period
        avg_l = (avg_l * (period - 1) + losses[i]) / period
    if avg_l == 0:
        return 100.0
    rs = avg_g / avg_l
    return 100.0 - 100.0 / (1.0 + rs)


def bollinger(
    closes: list[float], period: int, k: float
) -> tuple[float, float, float] | None:
    """Bollinger Bands (mid, upper, lower) over the last `period` closes (population std)."""
    if len(closes) < period:
        return None
    window = closes[-period:]
    mid = sum(window) / period
    var = sum((x - mid) ** 2 for x in window) / period
    sd = var**0.5
    return mid, mid + k * sd, mid - k * sd


def _ema_series(vals: list[float], period: int) -> list[float | None]:
    """EMA series aligned to `vals` (None until seeded at index period-1, SMA seed)."""
    n = len(vals)
    if n < period:
        return [None] * n
    kf = 2.0 / (period + 1)
    e = sum(vals[:period]) / period
    out: list[float | None] = [None] * (period - 1) + [e]
    for x in vals[period:]:
        e = x * kf + e * (1.0 - kf)
        out.append(e)
    return out


def ema(closes: list[float], period: int) -> float | None:
    """Latest EMA value (SMA-seeded). Needs `period` closes."""
    s = _ema_series(closes, period)
    return s[-1] if s and s[-1] is not None else None


def macd(
    closes: list[float], fast: int = 12, slow: int = 26, signal: int = 9
) -> tuple[float, float, float] | None:
    """MACD → (macd_line, signal_line, histogram) at the latest bar. Needs ~slow+signal bars."""
    if len(closes) < slow + signal:
        return None
    ef = _ema_series(closes, fast)
    es = _ema_series(closes, slow)
    macd_line = [
        (ef[i] - es[i]) if (ef[i] is not None and es[i] is not None) else None
        for i in range(len(closes))
    ]
    macd_vals = [m for m in macd_line if m is not None]
    if len(macd_vals) < signal:
        return None
    sig_series = _ema_series(macd_vals, signal)
    if not sig_series or sig_series[-1] is None:
        return None
    macd_now = float(macd_vals[-1])
    sig_now = float(sig_series[-1])
    return macd_now, sig_now, macd_now - sig_now
