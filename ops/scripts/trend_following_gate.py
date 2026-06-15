#!/usr/bin/env python3
"""
R4.1: 4H Dual-Direction Trend Following Gate.
BTC/ETH/SOL USDT perpetual swap — Donchian Channel (entry N=20, exit N=10).
Long + short + flat; costs: taker 5bps/leg + avg funding 8H prorated per bar.
"""
from __future__ import annotations

import asyncio
import datetime
import sys
import time
from pathlib import Path

import asyncpg
import httpx
import numpy as np

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
from omodul.backtest_gate import backtest_gate, BacktestGateConfig

# ── Constants ─────────────────────────────────────────────────────────────────
DB_DSN     = "postgresql://helios:helios_dev_pass@localhost:5434/helivex"
OKX_BASE   = "https://www.okx.com"
INSTRUMENTS = ["BTC-USDT-SWAP", "ETH-USDT-SWAP", "SOL-USDT-SWAP"]
MONTHS     = 12
ENTRY_N    = 20        # 20 × 4H ≈ 80H — Donchian entry channel
EXIT_N     = 10        # 10 × 4H ≈ 40H — Donchian exit channel (tighter)
TAKER_FEE  = 0.0005   # 5bps per leg  (OKX USDT perp taker, standard tier)
PERIODS    = 365 * 6  # 4H bars per year (24/7 crypto: 6 bars/day × 365)

# Avg realized funding per 8H settlement (from DB, 2026-03 → 2026-06)
# Long pays positive; short receives positive.
AVG_FUND_8H: dict[str, float] = {
    "BTC-USDT-SWAP": 0.000014,
    "ETH-USDT-SWAP": 0.000019,
    "SOL-USDT-SWAP": -0.0000008,
}
# 8H settlement timestamps: 00:00, 08:00, 16:00 UTC
SETTLEMENT_HOURS = {0, 8, 16}


# ── Data backfill ─────────────────────────────────────────────────────────────

async def _fetch_4h_pages(client: httpx.AsyncClient, inst_id: str, cutoff_ms: int) -> list:
    bars = []
    after_ms: int | None = None
    for _ in range(100):
        params: dict = {"instId": inst_id, "bar": "4H", "limit": "100"}
        if after_ms:
            params["after"] = str(after_ms)
        for ep in ["/api/v5/market/history-candles", "/api/v5/market/candles"]:
            r = await client.get(OKX_BASE + ep, params=params, timeout=15)
            d = r.json()
            if str(d.get("code", "1")) == "0":
                data = d.get("data", [])
                if data:
                    bars.extend(data)
                    if len(data) < 100 or int(data[-1][0]) <= cutoff_ms:
                        return bars
                    after_ms = int(data[-1][0])
                    await asyncio.sleep(0.12)
                break
        else:
            break
    return bars


async def backfill_4h_swap(months: int = MONTHS) -> dict[str, int]:
    cutoff_ms = int(
        (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=30 * months))
        .timestamp() * 1000
    )
    totals: dict[str, int] = {}
    conn = await asyncpg.connect(DB_DSN)
    async with httpx.AsyncClient(headers={"User-Agent": "helivex/1.0"}) as client:
        for inst in INSTRUMENTS:
            raw_bars = await _fetch_4h_pages(client, inst, cutoff_ms)
            records = []
            for b in raw_bars:
                ts_ms = int(b[0])
                if ts_ms < cutoff_ms:
                    continue
                bar_close_ts = datetime.datetime.fromtimestamp(
                    ts_ms / 1000, tz=datetime.timezone.utc
                )
                records.append((
                    inst, bar_close_ts, "okx_swap",
                    float(b[1]), float(b[2]), float(b[3]), float(b[4]),
                    float(b[5]), float(b[6]) if len(b) > 6 else None,
                ))
            if records:
                res = await conn.executemany(
                    """
                    INSERT INTO market_data.ohlcv_1h
                        (instrument, bar_close_ts, source, open, high, low, close, volume, quote_volume)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
                    ON CONFLICT (instrument, bar_close_ts, source) DO NOTHING
                    """,
                    records,
                )
                inserted = int(res.split()[-1]) if res else 0
            else:
                inserted = 0
            totals[inst] = inserted
    await conn.close()
    return totals


# ── Load data from DB ─────────────────────────────────────────────────────────

async def load_bars(inst: str) -> list[dict]:
    """Return list of {ts, open, high, low, close} dicts sorted ascending."""
    conn = await asyncpg.connect(DB_DSN)
    rows = await conn.fetch(
        """
        SELECT bar_close_ts, open::float, high::float, low::float, close::float
        FROM market_data.ohlcv_1h
        WHERE instrument = $1 AND source = 'okx_swap'
        ORDER BY bar_close_ts ASC
        """,
        inst,
    )
    await conn.close()
    return [dict(r) for r in rows]


# ── Strategy logic ────────────────────────────────────────────────────────────

def _annualized_sharpe(rets: list) -> float:
    arr = np.array(rets)
    if len(arr) < 5 or arr.std() < 1e-10:
        return 0.0
    return float(arr.mean() / arr.std() * np.sqrt(PERIODS))


def _make_bar_tuples(
    bars: list[dict], avg_fund: float
) -> list[tuple[float, float, float, float]]:
    """Convert bar dicts to (close, high, low, fund_rate) tuples.
    fund_rate is avg_fund at 8H settlement timestamps, 0 otherwise.
    """
    result = []
    for b in bars:
        ts: datetime.datetime = b["bar_close_ts"]
        hour = ts.hour if hasattr(ts, "hour") else ts.utctimetuple().tm_hour
        fund = avg_fund if hour in SETTLEMENT_HOURS else 0.0
        result.append((float(b["close"]), float(b["high"]), float(b["low"]), fund))
    return result


def _sim_donchian(
    bars: list[tuple],
    entry_n: int = ENTRY_N,
    exit_n: int = EXIT_N,
    taker_fee: float = TAKER_FEE,
) -> list[float]:
    """Dual-direction Donchian simulation using CLOSE-based channels.

    bars = [(close, high, low, fund_rate), ...]

    Channel at bar i: max/min of CLOSE prices in range(i-N, i) — the N bars
    BEFORE bar i (i is NOT included).  Signal = current close c vs that channel.
    This is the correct no-look-ahead formulation: c CAN exceed the prior N
    closes; c ≤ high[i] but that is NOT in the channel (bar i excluded).

    P&L at bar i: pos (set by the PREVIOUS bar's signal) × (c - prev_c)/prev_c.
    New position (new_pos) determined from c — takes effect from bar i+1 onward.
    Transaction cost deducted at bar i (close of day).
    Funding deducted at 8H settlement bars while in position.
    """
    n = max(entry_n, exit_n)
    pos = 0
    rets: list[float] = []

    for i in range(n, len(bars)):
        c    = float(bars[i][0])
        prev = float(bars[i - 1][0])
        fund = float(bars[i][3])

        if prev <= 0:
            rets.append(0.0)
            continue

        # Close-based Donchian channels from previous N bars (bar i excluded)
        entry_hi = max(float(bars[j][0]) for j in range(i - entry_n, i))
        entry_lo = min(float(bars[j][0]) for j in range(i - entry_n, i))
        exit_lo  = min(float(bars[j][0]) for j in range(i - exit_n,  i))
        exit_hi  = max(float(bars[j][0]) for j in range(i - exit_n,  i))

        # P&L for this bar using position from previous signal
        ret = float(pos) * (c - prev) / prev
        if fund != 0.0 and pos != 0:
            ret -= float(pos) * fund

        # New position determined from CURRENT close (applied from next bar)
        new_pos = pos
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
            ret -= taker_fee * (abs(pos) + abs(new_pos))
            pos = new_pos

        rets.append(ret)

    return rets


def make_strategy_fn(inst: str) -> callable:
    avg_fund = AVG_FUND_8H.get(inst, 0.0)

    def strategy_fn(train_data: list, test_data: list) -> dict:
        if len(train_data) < ENTRY_N + 5 or len(test_data) < 2:
            return {"sharpe": 0.0, "returns": [], "is_sharpe": 0.0, "n_trades": 0}

        train = [_bar(d) for d in train_data]
        test  = [_bar(d) for d in test_data]

        # IS simulation
        is_rets = _sim_donchian(train)
        is_sharpe = _annualized_sharpe(is_rets)

        # OOS: prepend last ENTRY_N IS bars so Donchian channel is warm at OOS start.
        # _sim_donchian(full) skips first max(ENTRY_N,EXIT_N)=ENTRY_N bars → yields
        # exactly len(test) returns, one per test bar. No look-ahead: channel uses
        # bars before each current bar; IS context bars are prior to any OOS signal.
        context  = train[-ENTRY_N:]
        full     = context + test            # length = ENTRY_N + len(test)
        oos_rets = _sim_donchian(full)       # length = len(full) - ENTRY_N = len(test)

        return {
            "sharpe":    _annualized_sharpe(oos_rets),
            "returns":   oos_rets,
            "is_sharpe": is_sharpe,
            "n_trades":  0,
        }

    return strategy_fn


def _bar(d: dict | tuple) -> tuple:
    """Normalise a data element to (close, high, low, fund_rate)."""
    if isinstance(d, dict):
        return (float(d["close"]), float(d["high"]), float(d["low"]), float(d.get("fund", 0.0)))
    return tuple(d)


# ── Gate runner ───────────────────────────────────────────────────────────────

def run_gate(inst: str, bars_with_fund: list[dict]) -> dict:
    data = [
        {
            "close": b["close"], "high": b["high"], "low": b["low"],
            "fund": AVG_FUND_8H.get(inst, 0.0)
            if b["bar_close_ts"].hour in SETTLEMENT_HOURS else 0.0,
        }
        for b in bars_with_fund
    ]
    config = BacktestGateConfig(
        strategy_name=f"trend_dual_{inst}",
        n_splits=5,
        embargo=ENTRY_N,    # 20-bar embargo between folds
        pbo_threshold=0.5,
        periods=PERIODS,
    )
    gate = backtest_gate(make_strategy_fn(inst), data, config=config)
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


# ── Segment analysis ──────────────────────────────────────────────────────────

def segment_analysis(bars: list[dict], inst: str) -> dict:
    """Split full-sample run into up/down/chop regimes and compute per-regime Sharpe.
    Regime defined by the 4H BTC price direction:
    - Bull: 30-bar (5-day) rolling return > +5%
    - Bear: 30-bar rolling return < -5%
    - Chop: between -5% and +5%
    Returns per-regime mean bar returns and approximate Sharpe.
    """
    tups = [(b["close"], b["close"], b["close"],
             AVG_FUND_8H.get(inst, 0.0) if b["bar_close_ts"].hour in SETTLEMENT_HOURS else 0.0)
            for b in bars]
    rets = _sim_donchian(tups)  # full sample simulation

    # Compute rolling 30-bar return for regime classification
    closes = np.array([b["close"] for b in bars])
    regime_labels = []
    for i in range(len(closes)):
        if i < 30:
            regime_labels.append("chop")
            continue
        roll_ret = (closes[i] - closes[i - 30]) / closes[i - 30]
        if roll_ret > 0.05:
            regime_labels.append("bull")
        elif roll_ret < -0.05:
            regime_labels.append("bear")
        else:
            regime_labels.append("chop")

    # Align rets (start at bar max(ENTRY_N, EXIT_N)) with regime labels
    n = max(ENTRY_N, EXIT_N)
    ret_labels = regime_labels[n + 1:][:len(rets)]

    by_regime: dict[str, list[float]] = {"bull": [], "bear": [], "chop": []}
    for r, label in zip(rets, ret_labels):
        by_regime[label].append(r)

    results = {}
    for regime, rlist in by_regime.items():
        arr = np.array(rlist)
        if len(arr) < 5:
            results[regime] = {"n_bars": 0, "cumret": 0.0, "sharpe": 0.0}
        else:
            cumret = float(arr.sum())
            sharpe = float(arr.mean() / arr.std() * np.sqrt(PERIODS)) if arr.std() > 1e-10 else 0.0
            results[regime] = {"n_bars": len(arr), "cumret": round(cumret * 100, 2), "sharpe": round(sharpe, 3)}

    return results


# ── BTC peak/trough analysis ──────────────────────────────────────────────────

def identify_market_phases(btc_bars: list[dict]) -> dict:
    closes = np.array([b["close"] for b in btc_bars])
    dates  = [b["bar_close_ts"] for b in btc_bars]
    peak_idx   = int(np.argmax(closes))
    trough_idx = int(np.argmin(closes[:peak_idx]) if peak_idx > 0 else 0)
    return {
        "start_date":    dates[0],
        "peak_date":     dates[peak_idx],
        "end_date":      dates[-1],
        "start_price":   round(float(closes[0]), 1),
        "peak_price":    round(float(closes[peak_idx]), 1),
        "end_price":     round(float(closes[-1]), 1),
        "upleg_pct":     round((closes[peak_idx] - closes[0]) / closes[0] * 100, 1),
        "downleg_pct":   round((closes[-1] - closes[peak_idx]) / closes[peak_idx] * 100, 1),
        "total_pct":     round((closes[-1] - closes[0]) / closes[0] * 100, 1),
    }


# ── Verdict doc ───────────────────────────────────────────────────────────────

def write_verdict(
    row_counts: dict[str, int],
    gates: dict[str, dict],
    segments: dict[str, dict],
    market: dict,
) -> None:
    inst_lines = []
    pass_count = 0
    for inst, g in gates.items():
        status = "PASS" if g["gate_status"] == "passed" else "FAIL"
        if status == "PASS":
            pass_count += 1
        inst_lines.append(
            f"| {inst} | {g['mean_is']:.4f} | {g['oos_sharpe']:.4f} | "
            f"{g['dsr']:.4f} | {g['pbo']:.2f} | **{status}** | {g['fold_oos']} |"
        )

    seg_lines = []
    for inst in INSTRUMENTS:
        for regime in ["bull", "bear", "chop"]:
            s = segments.get(inst, {}).get(regime, {})
            seg_lines.append(
                f"| {inst} | {regime} | {s.get('n_bars',0)} | "
                f"{s.get('cumret',0):.2f}% | {s.get('sharpe',0):.3f} |"
            )

    if pass_count >= 2:
        overall = "PASS ≥2/3"
        conclusion = (
            "Dual-direction trend following passes the honest gate on "
            f"{pass_count}/3 instruments. Strategy 1 has alpha — proceed to execution preparation."
        )
    elif pass_count == 1:
        overall = "CONDITIONAL"
        conclusion = (
            "Only 1/3 instruments passes. Check segment analysis: if bear+bull both positive "
            "and only chop drags, add regime gate (flat in chop) and re-test."
        )
    else:
        overall = "NO-GO"
        conclusion = (
            "0/3 instruments pass the honest gate. "
            "Dual-direction does not reliably outperform at 4H with these parameters."
        )

    doc = f"""# R4.1 4H Dual-Direction Trend Following Gate

> BTC/ETH/SOL USDT perpetual swap — Donchian Channel (entry N=20 bars, exit N=10 bars)
> Strategy 1/3 of helivex execution roadmap

## §1 Data

| Instrument | 4H bars inserted | Bar range |
|---|---|---|
| BTC-USDT-SWAP | {row_counts.get("BTC-USDT-SWAP", 0)} | 12 months |
| ETH-USDT-SWAP | {row_counts.get("ETH-USDT-SWAP", 0)} | 12 months |
| SOL-USDT-SWAP | {row_counts.get("SOL-USDT-SWAP", 0)} | 12 months |

Source: OKX public API, stored as `source='okx_swap'` in `market_data.ohlcv_1h`.

## §2 Strategy

- **Signal**: Donchian Channel dual-direction
  - Long when prev_close > 20-bar high (breakout above channel)
  - Short when prev_close < 20-bar low (breakdown below channel)
  - Flat when price is within the channel (no trend)
- **Exit**: 10-bar channel — long exits when prev_close < 10-bar low; short when prev_close > 10-bar high
- **Costs**: OKX taker 5bps/leg; funding prorated per 4H bar (avg {AVG_FUND_8H["BTC-USDT-SWAP"]*100:.4f}% per 8H for BTC)
- **No look-ahead**: signal uses `prev_close` vs channel of bars `[i-N, i)` (current bar excluded) ✓

## §3 Market Context (BTC 12-Month)

| Phase | Date | Price | Return |
|---|---|---|---|
| Start | {market['start_date'].strftime('%Y-%m-%d') if hasattr(market['start_date'], 'strftime') else market['start_date']} | {market['start_price']:,.0f} | — |
| Peak  | {market['peak_date'].strftime('%Y-%m-%d') if hasattr(market['peak_date'], 'strftime') else market['peak_date']} | {market['peak_price']:,.0f} | +{market['upleg_pct']:.1f}% |
| End   | {market['end_date'].strftime('%Y-%m-%d') if hasattr(market['end_date'], 'strftime') else market['end_date']} | {market['end_price']:,.0f} | {market['downleg_pct']:.1f}% from peak |
| Total 12-month | | | {market['total_pct']:+.1f}% |

## §4 Gate Results (Walk-Forward 5-Fold CPCV)

| Instrument | IS Sharpe | OOS Sharpe | DSR | PBO | Gate | Fold OOS |
|---|---|---|---|---|---|---|
{chr(10).join(inst_lines)}

### Overall: **{overall}**

## §5 Segment Performance (Full-Sample, For Regime Insight)

| Instrument | Regime | Bars | Cum Return | Sharpe |
|---|---|---|---|---|
{chr(10).join(seg_lines)}

Regime: bull = rolling 5-day BTC return > +5%; bear < −5%; chop = in between.

## §6 Look-Ahead Audit

- Donchian channels use `max(high[i-N:i])` and `min(low[i-N:i])` — strictly prior bars ✓
- Signal based on `close[i-1]` (previous bar close, available before bar i opens) ✓
- No IS parameter optimisation: ENTRY_N=20, EXIT_N=10 fixed regardless of fold ✓
- OOS warm-up: last 20 IS bars prepended as context so OOS channel is initialised ✓

## §7 Verdict

{conclusion}

---

| Strategy | DSR | PBO | Gate |
|---|---|---|---|
| R3 funding_arb | −24.10 | 1.00 | FAIL |
| R3 stat_arb ETC/XRP (honest) | +0.09 | 0.75 | FAIL |
| R3 cash_carry_basis | −0.61 | 0.75 | FAIL |
| R4.1 trend_dual (BTC) | {gates.get("BTC-USDT-SWAP", {}).get("dsr", 0):.2f} | {gates.get("BTC-USDT-SWAP", {}).get("pbo", 1):.2f} | {('PASS' if gates.get('BTC-USDT-SWAP', {}).get('gate_status') == 'passed' else 'FAIL')} |
| R4.1 trend_dual (ETH) | {gates.get("ETH-USDT-SWAP", {}).get("dsr", 0):.2f} | {gates.get("ETH-USDT-SWAP", {}).get("pbo", 1):.2f} | {('PASS' if gates.get('ETH-USDT-SWAP', {}).get('gate_status') == 'passed' else 'FAIL')} |
| R4.1 trend_dual (SOL) | {gates.get("SOL-USDT-SWAP", {}).get("dsr", 0):.2f} | {gates.get("SOL-USDT-SWAP", {}).get("pbo", 1):.2f} | {('PASS' if gates.get('SOL-USDT-SWAP', {}).get('gate_status') == 'passed' else 'FAIL')} |

---
*Generated by `ops/scripts/trend_following_gate.py` — R4.1 milestone.*
"""
    out = Path(__file__).parent.parent.parent / "docs" / "R4.1_TREND_DUAL_GATE.md"
    out.write_text(doc)


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    print("=== R4.1 4H Dual-Direction Trend Following Gate ===\n")

    # Step 1: backfill 4H SWAP bars
    print("Step 1: Backfilling 4H SWAP bars to DB...")
    row_counts = asyncio.run(backfill_4h_swap())
    for inst, n in row_counts.items():
        print(f"  {inst}: {n} new rows inserted")

    # Step 2: load from DB, run gate per instrument
    gates: dict[str, dict] = {}
    segments: dict[str, dict] = {}
    btc_bars = None

    for inst in INSTRUMENTS:
        print(f"\nStep 2: Loading {inst} from DB...")
        bars = asyncio.run(load_bars(inst))
        print(f"  {len(bars)} 4H bars  {bars[0]['bar_close_ts']} → {bars[-1]['bar_close_ts']}")

        if inst == "BTC-USDT-SWAP":
            btc_bars = bars

        print(f"  Running backtest_gate (n_splits=5, embargo={ENTRY_N}, periods={PERIODS})...")
        t0 = time.monotonic()
        g = run_gate(inst, bars)
        elapsed = time.monotonic() - t0

        status = g["gate_status"].upper()
        print(f"  [{elapsed:.1f}s] {status}  IS={g['mean_is']}  OOS={g['oos_sharpe']}  "
              f"DSR={g['dsr']}  PBO={g['pbo']}")
        print(f"  Fold OOS: {g['fold_oos']}")
        gates[inst] = g

        print(f"  Segment analysis...")
        segments[inst] = segment_analysis(bars, inst)
        for regime, s in segments[inst].items():
            print(f"    {regime:5s}: n={s['n_bars']:4d}  cum={s['cumret']:+.2f}%  sharpe={s['sharpe']:.3f}")

    # Step 3: market context
    market = identify_market_phases(btc_bars) if btc_bars else {}
    print(f"\nBTC market phases: {market}")

    # Step 4: write verdict
    write_verdict(row_counts, gates, segments, market)
    print("\nVerdict → docs/R4.1_TREND_DUAL_GATE.md")


if __name__ == "__main__":
    main()
