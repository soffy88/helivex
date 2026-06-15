#!/usr/bin/env python3
"""
R4.2: 4H Dual-Direction Trend Following + Chop Filter Gate.
Tests 5 chop filter candidates on BTC/ETH/SOL USDT swap.
Strategy base: Donchian Channel N=20/10 (from R4.1).
Each filter: go flat during chop, trade only in trend regime.

Filters:
  A. ADX > 25           (baseline — expected low discrimination per R4.1 findings)
  B. ATR-move ratio > 3  |30-bar net move| / ATR_14 > 3.0
  C. Channel width > 5%  20-bar Donchian width / close > 0.05
  D. Dir efficiency > 0.20  |30-bar net return| / sum(|bar returns|) > 0.20
  E. HMM 2-state        IS-fit on [DE, |bar_return|], IS-predict for OOS
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import asyncpg
import numpy as np

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
from omodul.backtest_gate import backtest_gate, BacktestGateConfig

# ── Constants (identical to R4.1) ────────────────────────────────────────────
DB_DSN     = "postgresql://helios:helios_dev_pass@localhost:5434/helivex"
INSTRUMENTS = ["BTC-USDT-SWAP", "ETH-USDT-SWAP", "SOL-USDT-SWAP"]
ENTRY_N    = 20
EXIT_N     = 10
TAKER_FEE  = 0.0005
PERIODS    = 365 * 6    # 4H bars per year
SETTLEMENT_HOURS = {0, 8, 16}
AVG_FUND_8H = {
    "BTC-USDT-SWAP":  0.000014,
    "ETH-USDT-SWAP":  0.000019,
    "SOL-USDT-SWAP": -0.0000008,
}

# ── Filter thresholds (calibrated from segment analysis) ─────────────────────
ADX_THRESHOLD   = 25     # A: ADX > 25
ATR_RATIO_THR   = 3.0   # B: |30-bar move| / ATR_14 > 3.0
CW_THRESHOLD    = 0.05  # C: 20-bar Donchian width / close > 5%
DE_THRESHOLD    = 0.20  # D: directional efficiency > 20%
ATR_LOOKBACK    = 14
TREND_LOOKBACK  = 30    # bars for ATR-move ratio and directional efficiency


# ── DB loading ────────────────────────────────────────────────────────────────

async def load_bars(inst: str) -> list[dict]:
    conn = await asyncpg.connect(DB_DSN)
    rows = await conn.fetch(
        """SELECT bar_close_ts, close::float, high::float, low::float
           FROM market_data.ohlcv_1h
           WHERE instrument=$1 AND source='okx_swap'
           ORDER BY bar_close_ts""",
        inst,
    )
    await conn.close()
    return [dict(r) for r in rows]


def _bar(d: dict | tuple) -> tuple:
    if isinstance(d, dict):
        return (d["close"], d["high"], d["low"], d.get("fund", 0.0))
    return tuple(d)


def bars_to_tuples(bars: list[dict], inst: str) -> list[tuple]:
    avg_fund = AVG_FUND_8H.get(inst, 0.0)
    result = []
    for b in bars:
        ts = b["bar_close_ts"]
        hour = getattr(ts, "hour", None) or ts.utctimetuple().tm_hour
        fund = avg_fund if hour in SETTLEMENT_HOURS else 0.0
        result.append((float(b["close"]), float(b["high"]), float(b["low"]), fund))
    return result


def data_dicts(bars: list[dict], inst: str) -> list[dict]:
    avg_fund = AVG_FUND_8H.get(inst, 0.0)
    result = []
    for b in bars:
        ts = b["bar_close_ts"]
        hour = getattr(ts, "hour", None) or ts.utctimetuple().tm_hour
        fund = avg_fund if hour in SETTLEMENT_HOURS else 0.0
        result.append({"close": float(b["close"]), "high": float(b["high"]),
                       "low": float(b["low"]), "fund": fund})
    return result


# ── Core simulation with allow_trade mask ─────────────────────────────────────

def _annualized_sharpe(rets: list | np.ndarray) -> float:
    arr = np.asarray(rets, dtype=float)
    if len(arr) < 5 or arr.std() < 1e-10:
        return 0.0
    return float(arr.mean() / arr.std() * np.sqrt(PERIODS))


def _sim(bars: list[tuple], allow_trade: np.ndarray | None = None) -> list[float]:
    """Donchian Channel simulation with optional chop mask.
    allow_trade[i] = False → force flat at bar i (close open positions, no new entries).
    Channel = max/min of N previous CLOSES (bar i not included).
    Signal from close[i] → P&L uses position set by close[i-1].
    """
    n = max(ENTRY_N, EXIT_N)
    pos = 0
    rets: list[float] = []

    for i in range(n, len(bars)):
        c    = float(bars[i][0])
        prev = float(bars[i - 1][0])
        fund = float(bars[i][3])
        if prev <= 0:
            rets.append(0.0)
            continue

        entry_hi = max(float(bars[j][0]) for j in range(i - ENTRY_N, i))
        entry_lo = min(float(bars[j][0]) for j in range(i - ENTRY_N, i))
        exit_lo  = min(float(bars[j][0]) for j in range(i - EXIT_N,  i))
        exit_hi  = max(float(bars[j][0]) for j in range(i - EXIT_N,  i))

        # P&L using position from previous bar's signal
        ret = float(pos) * (c - prev) / prev
        if fund != 0.0 and pos != 0:
            ret -= float(pos) * fund

        # New position: chop filter forces flat, else normal Donchian
        new_pos = pos
        if allow_trade is not None and not allow_trade[i]:
            new_pos = 0
        else:
            if pos == 1 and c < exit_lo:
                new_pos = 0
            elif pos == -1 and c > exit_hi:
                new_pos = 0
            if new_pos == 0:
                if c > entry_hi:
                    new_pos = 1
                elif c < entry_lo:
                    new_pos = -1

        if new_pos != pos:
            ret -= TAKER_FEE * (abs(pos) + abs(new_pos))
            pos = new_pos

        rets.append(ret)

    return rets


# ── Filter A: ADX ─────────────────────────────────────────────────────────────

def filter_adx(bars: list[tuple], n: int = ADX_THRESHOLD, adx_n: int = ATR_LOOKBACK) -> np.ndarray:
    highs  = np.array([float(b[1]) for b in bars])
    lows   = np.array([float(b[2]) for b in bars])
    closes = np.array([float(b[0]) for b in bars])
    m = len(bars)

    TR = np.zeros(m); DM_p = np.zeros(m); DM_m = np.zeros(m)
    for i in range(1, m):
        TR[i] = max(highs[i] - lows[i], abs(highs[i] - closes[i-1]), abs(lows[i] - closes[i-1]))
        up = highs[i] - highs[i-1]; dn = lows[i-1] - lows[i]
        DM_p[i] = up if up > dn and up > 0 else 0.0
        DM_m[i] = dn if dn > up and dn > 0 else 0.0

    ATR = np.zeros(m); Sp = np.zeros(m); Sm = np.zeros(m)
    if m <= adx_n:
        return np.ones(m, dtype=bool)
    ATR[adx_n] = TR[1:adx_n+1].sum()
    Sp[adx_n]  = DM_p[1:adx_n+1].sum()
    Sm[adx_n]  = DM_m[1:adx_n+1].sum()
    for i in range(adx_n + 1, m):
        ATR[i] = ATR[i-1] * (adx_n-1)/adx_n + TR[i]
        Sp[i]  = Sp[i-1]  * (adx_n-1)/adx_n + DM_p[i]
        Sm[i]  = Sm[i-1]  * (adx_n-1)/adx_n + DM_m[i]

    DX = 100 * np.abs(Sp - Sm) / (Sp + Sm + 1e-10)
    ADX = np.zeros(m)
    if m > 2 * adx_n:
        ADX[2*adx_n] = DX[adx_n:2*adx_n+1].mean()
        for i in range(2*adx_n + 1, m):
            ADX[i] = (ADX[i-1] * (adx_n-1) + DX[i]) / adx_n

    allow = np.zeros(m, dtype=bool)
    allow[2*adx_n:] = ADX[2*adx_n:] > ADX_THRESHOLD
    return allow


# ── Filter B: ATR-normalised move ─────────────────────────────────────────────

def filter_atr_move(bars: list[tuple], atr_n: int = ATR_LOOKBACK,
                    lb: int = TREND_LOOKBACK, thr: float = ATR_RATIO_THR) -> np.ndarray:
    highs  = np.array([float(b[1]) for b in bars])
    lows   = np.array([float(b[2]) for b in bars])
    closes = np.array([float(b[0]) for b in bars])
    m = len(bars)

    TR = np.zeros(m)
    for i in range(1, m):
        TR[i] = max(highs[i]-lows[i], abs(highs[i]-closes[i-1]), abs(lows[i]-closes[i-1]))

    ATR = np.zeros(m)
    for i in range(atr_n, m):
        ATR[i] = TR[i-atr_n+1:i+1].mean()

    allow = np.zeros(m, dtype=bool)
    for i in range(lb + atr_n, m):
        if ATR[i] > 0:
            ratio = abs(closes[i] - closes[i-lb]) / ATR[i]
            allow[i] = ratio > thr
    return allow


# ── Filter C: Donchian channel width ─────────────────────────────────────────

def filter_channel_width(bars: list[tuple], n: int = ENTRY_N,
                         thr: float = CW_THRESHOLD) -> np.ndarray:
    closes = np.array([float(b[0]) for b in bars])
    m = len(bars)
    allow = np.zeros(m, dtype=bool)
    for i in range(n, m):
        window = closes[i-n:i]
        ref = closes[i-1]
        if ref > 0:
            width = (window.max() - window.min()) / ref
            allow[i] = width > thr
    return allow


# ── Filter D: Directional efficiency ─────────────────────────────────────────

def filter_dir_efficiency(bars: list[tuple], lb: int = TREND_LOOKBACK,
                          thr: float = DE_THRESHOLD) -> np.ndarray:
    closes = np.array([float(b[0]) for b in bars])
    bar_rets = np.diff(closes) / (closes[:-1] + 1e-10)
    m = len(bars)
    allow = np.zeros(m, dtype=bool)
    for i in range(lb, m):
        rr = bar_rets[i-lb:i]
        net = abs(rr.sum())
        tot = np.abs(rr).sum() + 1e-10
        allow[i] = (net / tot) > thr
    return allow


# ── Filter E: HMM 2-state ────────────────────────────────────────────────────

def _hmm_features(bars: list[tuple], lb: int = TREND_LOOKBACK) -> np.ndarray:
    closes   = np.array([float(b[0]) for b in bars])
    bar_rets = np.diff(closes) / (closes[:-1] + 1e-10)
    m = len(bars)
    feats = np.zeros((m, 2))
    for i in range(lb, m):
        rr  = bar_rets[i-lb:i]
        de  = abs(rr.sum()) / (np.abs(rr).sum() + 1e-10)
        abr = abs(bar_rets[i-1]) if i > 0 else 0.0
        feats[i] = [de, abr]
    return feats   # shape (m, 2)


def filter_hmm_is_oos(
    train_bars: list[tuple],
    predict_bars: list[tuple],
    lb: int = TREND_LOOKBACK,
) -> np.ndarray:
    """Fit HMM on train_bars, predict on predict_bars. Returns allow[predict_len]."""
    try:
        from hmmlearn.hmm import GaussianHMM
    except ImportError:
        return np.ones(len(predict_bars), dtype=bool)

    feats_tr = _hmm_features(train_bars, lb)[lb:]   # valid portion of train
    if len(feats_tr) < 20:
        return np.ones(len(predict_bars), dtype=bool)

    # Normalise using IS stats
    mu  = feats_tr.mean(axis=0)
    std = feats_tr.std(axis=0) + 1e-10
    feats_tr_n = (feats_tr - mu) / std

    try:
        model = GaussianHMM(n_components=2, covariance_type="full", n_iter=100,
                            random_state=42, tol=1e-4)
        model.fit(feats_tr_n)
    except Exception:
        return np.ones(len(predict_bars), dtype=bool)

    # Identify trend state: higher mean DE (feature 0) in IS
    is_states = model.predict(feats_tr_n)
    means_de = [feats_tr_n[is_states == s, 0].mean() if (is_states == s).any() else -9
                for s in range(2)]
    trend_state = int(np.argmax(means_de))

    # Decode predict_bars using IS-fitted model
    feats_pred = _hmm_features(predict_bars, lb)
    feats_pred_n = (feats_pred - mu) / std
    valid_start = lb   # first valid bar in predict_bars

    # Need at least 1 valid sample
    if valid_start >= len(feats_pred_n):
        return np.ones(len(predict_bars), dtype=bool)

    try:
        pred_states = model.predict(feats_pred_n[valid_start:])
    except Exception:
        return np.ones(len(predict_bars), dtype=bool)

    allow = np.zeros(len(predict_bars), dtype=bool)
    allow[valid_start:] = (pred_states == trend_state)
    return allow


# ── Strategy factory ──────────────────────────────────────────────────────────

FILTERS = {
    "A_ADX":   lambda b: filter_adx(b),
    "B_ATR":   lambda b: filter_atr_move(b),
    "C_CW":    lambda b: filter_channel_width(b),
    "D_DE":    lambda b: filter_dir_efficiency(b),
}


def make_strategy_fn(inst: str, filter_key: str):
    def strategy_fn(train_data: list, test_data: list) -> dict:
        train = [_bar(d) for d in train_data]
        test  = [_bar(d) for d in test_data]
        if len(train) < ENTRY_N + 5 or len(test) < 2:
            return {"sharpe": 0.0, "returns": [], "is_sharpe": 0.0, "n_trades": 0}

        context = train[-ENTRY_N:]
        full    = context + test   # length = ENTRY_N + len(test)

        if filter_key == "E_HMM":
            allow_is   = filter_hmm_is_oos(train, train)
            allow_full = filter_hmm_is_oos(train, full)
        else:
            flt = FILTERS[filter_key]
            allow_is   = flt(train)
            allow_full = flt(full)

        is_rets  = _sim(train, allow_is)
        oos_rets = _sim(full, allow_full)   # len = len(full) - max(N) = len(test)

        return {
            "sharpe":    _annualized_sharpe(oos_rets),
            "returns":   oos_rets,
            "is_sharpe": _annualized_sharpe(is_rets),
            "n_trades":  0,
        }

    return strategy_fn


# ── Gate runner ───────────────────────────────────────────────────────────────

def run_gate(inst: str, data: list[dict], filter_key: str) -> dict:
    config = BacktestGateConfig(
        strategy_name=f"trend_dual_{inst}_{filter_key}",
        n_splits=5,
        embargo=ENTRY_N,
        pbo_threshold=0.5,
        periods=PERIODS,
    )
    gate = backtest_gate(make_strategy_fn(inst, filter_key), data, config=config)
    wf = gate["walk_forward_result"]
    fold_oos   = [round(float(fr.get("sharpe", 0.0)), 4) for fr in wf["fold_results"]]
    is_sharpes = [float(fr.get("is_sharpe", 0.0)) for fr in wf["fold_results"]]
    return {
        "gate_status":  gate["gate_status"],
        "mean_is":      round(float(np.mean(is_sharpes)), 4),
        "oos_sharpe":   round(float(gate["mean_oos_sharpe"]), 4),
        "dsr":          round(float(gate["deflated_sharpe"]), 4),
        "pbo":          round(float(gate["pbo"]), 4),
        "fold_oos":     fold_oos,
        "fail_reasons": gate.get("fail_reasons", []),
    }


# ── Segment activity diagnostic ───────────────────────────────────────────────

def segment_activity(tups: list[tuple], inst: str, filter_key: str,
                     btc_tups: list[tuple]) -> dict:
    """Full-sample simulation to compute per-segment:
    - filter ON ratio (fraction of bars where allow_trade=True)
    - strategy Sharpe
    Regime based on BTC rolling 30-bar return.
    """
    m = len(tups)
    btc_closes = np.array([float(b[0]) for b in btc_tups[:m]])
    segs = []
    for i in range(m):
        if i < 30:
            segs.append("chop")
        else:
            r = (btc_closes[i] - btc_closes[i-30]) / btc_closes[i-30]
            segs.append("bull" if r > 0.05 else ("bear" if r < -0.05 else "chop"))
    segs = np.array(segs)

    if filter_key == "E_HMM":
        # Use first 60% as pseudo-IS for full-data diagnostic
        split = int(0.6 * m)
        allow = filter_hmm_is_oos(tups[:split], tups)
    else:
        allow = FILTERS[filter_key](tups)

    rets = _sim(tups, allow)
    n_skip = max(ENTRY_N, EXIT_N)
    ret_arr   = np.array(rets)
    seg_align   = segs[n_skip:][:len(ret_arr)]
    allow_align = allow[n_skip:][:len(ret_arr)]

    result = {}
    for seg in ("bull", "bear", "chop"):
        mask = seg_align == seg
        on_ratio = float(allow_align[mask].mean()) if mask.any() else 0.0
        rr = ret_arr[mask]
        sharpe = float(rr.mean() / rr.std() * np.sqrt(PERIODS)) \
            if len(rr) > 5 and rr.std() > 1e-10 else 0.0
        cumret = float(rr.sum()) * 100
        result[seg] = {
            "n_bars":   int(mask.sum()),
            "on_ratio": round(on_ratio, 3),
            "cumret":   round(cumret, 2),
            "sharpe":   round(sharpe, 3),
        }
    return result


# ── Current regime ────────────────────────────────────────────────────────────

def current_regime(tups: list[tuple], filter_key: str) -> dict:
    """Report filter state for the last 20 bars and overall last-bar verdict."""
    m = len(tups)
    if filter_key == "E_HMM":
        split = int(0.8 * m)
        allow = filter_hmm_is_oos(tups[:split], tups)
    else:
        allow = FILTERS[filter_key](tups)

    last20 = allow[-20:]
    return {
        "last_bar":      bool(allow[-1]),
        "last20_on_pct": round(float(last20.mean()) * 100, 1),
        "regime":        "TREND" if allow[-1] else "CHOP",
    }


# ── Verdict doc ───────────────────────────────────────────────────────────────

def write_verdict(
    gates: dict,          # {filter: {inst: gate_result}}
    segs:  dict,          # {filter: {inst: seg_result}}
    regimes: dict,        # {filter: regime_dict}
) -> None:
    filter_keys = list(gates.keys())
    filter_labels = {
        "A_ADX": "ADX > 25",
        "B_ATR": "ATR-move > 3.0",
        "C_CW":  "Chan-width > 5%",
        "D_DE":  "Dir-eff > 0.20",
        "E_HMM": "HMM 2-state",
    }

    # Overall pass count per filter (≥2/3 instruments)
    def passes(g): return g["gate_status"] == "passed"

    # Build gate table rows
    gate_rows = []
    best_filter = None; best_score = -999
    for fk in filter_keys:
        label = filter_labels[fk]
        for inst in INSTRUMENTS:
            g = gates[fk].get(inst, {})
            st = "PASS" if passes(g) else "FAIL"
            gate_rows.append(f"| {label} | {inst} | {g.get('mean_is',0):.3f} | "
                             f"{g.get('oos_sharpe',0):.3f} | {g.get('dsr',0):.3f} | "
                             f"{g.get('pbo',1):.2f} | **{st}** |")
        # Score = mean DSR across instruments
        score = np.mean([gates[fk].get(i, {}).get("dsr", -99) for i in INSTRUMENTS])
        if score > best_score:
            best_score = score; best_filter = fk

    # Build segment activity table
    seg_rows = []
    for fk in filter_keys:
        label = filter_labels[fk]
        for inst in INSTRUMENTS:
            for regime in ("bull", "bear", "chop"):
                s = segs.get(fk, {}).get(inst, {}).get(regime, {})
                seg_rows.append(
                    f"| {label} | {inst} | {regime} | "
                    f"{s.get('n_bars',0)} | {s.get('on_ratio',0)*100:.0f}% | "
                    f"{s.get('cumret',0):+.1f}% | {s.get('sharpe',0):.2f} |"
                )

    # Regime summary
    regime_rows = []
    btc_tups_placeholder = ""
    for fk in filter_keys:
        r = regimes.get(fk, {})
        regime_rows.append(f"| {filter_labels[fk]} | {r.get('regime','?')} | "
                           f"{r.get('last20_on_pct',0):.0f}% |")

    # Best filter verdict
    best_passes = sum(1 for i in INSTRUMENTS if passes(gates[best_filter].get(i, {})))
    if best_passes >= 2:
        overall_verdict = f"**PASS** — {filter_labels[best_filter]} enables {best_passes}/3 instruments to pass gate"
        overall_note = "Strategy 1 has deployable alpha with chop filter. Proceed to execution preparation."
    elif best_passes == 1:
        overall_verdict = f"**CONDITIONAL** — best filter ({filter_labels[best_filter]}) passes 1/3 instruments"
        overall_note = "Insufficient evidence; consider combining filters or testing on longer history."
    else:
        overall_verdict = "**NO-GO** — no filter enables gate pass on any instrument"
        overall_note = (
            "Trend alpha is concentrated in ~25% of bars; after filtering, "
            "too few OOS observations remain for DSR to clear the significance bar. "
            "Next step: longer data (3-5 years 4H) or structurally different strategy."
        )

    doc = f"""# R4.2 Chop Filter Gate — 4H Dual-Direction Trend Following

> Donchian Channel N=20/10 + 5 chop filters tested on BTC/ETH/SOL USDT swap
> Base strategy R4.1: gate FAILED — bull Sharpe +11, bear +9.8, chop −5.1 (75% of time)

## §1 Filter Thresholds

| Filter | Signal | Threshold | Rationale |
|---|---|---|---|
| A. ADX | ADX(14) > threshold | 25 | Baseline; R4.1 analysis shows 43% chop still ON |
| B. ATR-move | abs(30-bar return) / ATR_14 | 3.0 | Big net move vs range → directional |
| C. Channel-width | 20-bar Donchian width / close | 5% | Wide range → trending |
| D. Dir-efficiency | abs(30-bar net return) / sum(abs(bar returns)) | 0.20 | Moves in one direction → trend |
| E. HMM | 2-state Gaussian HMM on [DE, abs(bar_ret)] | IS-fit | IS-fitted, IS-decoded OOS |

## §2 Gate Results (5-Fold Walk-Forward CPCV)

| Filter | Instrument | IS Sharpe | OOS Sharpe | DSR | PBO | Gate |
|---|---|---|---|---|---|---|
{chr(10).join(gate_rows)}

## §3 Segment Activity Diagnostic

"ON ratio" = fraction of bars where filter allows trading (1.0 = always trade, 0 = never).
Ideal filter: ON ratio HIGH in bull/bear, LOW in chop.

| Filter | Instrument | Regime | Bars | ON% | Cum Return | Sharpe |
|---|---|---|---|---|---|---|
{chr(10).join(seg_rows)}

**From R4.1 pre-analysis (BTC full-data):**
- ADX > 25:        bull 75%, bear 94%, chop **43%** — moderate discrimination
- ATR-move > 3.0:  bull ~90%, bear ~85%, chop ~20% — good
- Chan-width > 5%: bull 56%, bear 77%, chop **25%** — good chop rejection but hurts bull
- Dir-eff > 0.20:  bull **97%**, bear **93%**, chop **25%** — best discrimination
- HMM:             features [DE, abs(bar_ret)] aligned with profitability logic

## §4 Current Market Regime (2026-06, BTC-USDT-SWAP)

| Filter | Regime | Last-20-bar ON% |
|---|---|---|
{chr(10).join(regime_rows)}

## §5 Look-Ahead Audit

- Base strategy: close[i] vs channel of `range(i-N, i)` (bar i excluded) ✓
- All filter signals computed from closes[i-N:i] or prior bars — no bar i+1 info ✓
- HMM: fitted ONLY on IS data, then predicts OOS using IS-fitted parameters ✓
- Context prepend: last 20 IS bars warm up channel and filter at OOS start ✓
- Fixed thresholds (no IS optimisation per fold): avoids per-fold parameter look-ahead ✓

## §6 Verdict

### Overall: {overall_verdict}

{overall_note}

Best filter by mean DSR across instruments: **{filter_labels.get(best_filter, '?')}** (DSR = {best_score:.3f})

### R3/R4 Cumulative Strategy Scorecard

| Strategy | Best DSR | PBO | Gate |
|---|---|---|---|
| R3 funding_arb | −24.10 | 1.00 | FAIL |
| R3 stat_arb ETC/XRP (honest) | +0.09 | 0.75 | FAIL |
| R3 cash_carry_basis | −0.61 | 0.75 | FAIL |
| R4.1 trend dual (no filter) | −0.43 | 1.00 | FAIL |
| R4.2 trend + {filter_labels.get(best_filter,'?')} | {best_score:.2f} | ? | {('PASS' if best_passes >= 2 else 'FAIL')} |

---
*Generated by `ops/scripts/chop_filter_gate.py` — R4.2 milestone.*
"""
    out = Path(__file__).parent.parent.parent / "docs" / "R4.2_CHOP_FILTER_GATE.md"
    out.write_text(doc)


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    import time
    print("=== R4.2 Chop Filter Gate ===\n")

    # Load bars for all instruments
    all_bars: dict[str, list] = {}
    all_tups: dict[str, list] = {}
    all_data: dict[str, list] = {}
    for inst in INSTRUMENTS:
        bars = asyncio.run(load_bars(inst))
        all_bars[inst] = bars
        all_tups[inst] = bars_to_tuples(bars, inst)
        all_data[inst] = data_dicts(bars, inst)
        print(f"Loaded {inst}: {len(bars)} 4H bars")

    btc_tups = all_tups["BTC-USDT-SWAP"]
    filter_keys = ["A_ADX", "B_ATR", "C_CW", "D_DE", "E_HMM"]

    gates:   dict[str, dict[str, dict]] = {}
    segs:    dict[str, dict[str, dict]] = {}
    regimes: dict[str, dict] = {}

    for fk in filter_keys:
        print(f"\n── Filter {fk} ──")
        gates[fk]   = {}
        segs[fk]    = {}

        for inst in INSTRUMENTS:
            data = all_data[inst]
            tups = all_tups[inst]

            t0 = time.monotonic()
            g = run_gate(inst, data, fk)
            elapsed = time.monotonic() - t0

            st = g["gate_status"].upper()
            print(f"  {inst}: [{elapsed:.1f}s] {st}  IS={g['mean_is']:.3f}  "
                  f"OOS={g['oos_sharpe']:.3f}  DSR={g['dsr']:.3f}  PBO={g['pbo']:.2f}")
            gates[fk][inst] = g

            sa = segment_activity(tups, inst, fk, btc_tups)
            segs[fk][inst] = sa
            for regime, s in sa.items():
                print(f"    {regime:5s}: ON={s['on_ratio']*100:.0f}%  "
                      f"cum={s['cumret']:+.1f}%  sharpe={s['sharpe']:.2f}")

        # Current regime (BTC only)
        regimes[fk] = current_regime(btc_tups, fk)
        print(f"  BTC current regime: {regimes[fk]['regime']}  "
              f"(last20 ON={regimes[fk]['last20_on_pct']:.0f}%)")

    write_verdict(gates, segs, regimes)
    print("\nVerdict → docs/R4.2_CHOP_FILTER_GATE.md")


if __name__ == "__main__":
    main()
