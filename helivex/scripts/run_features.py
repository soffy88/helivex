"""Step 2 — feature + regime pipeline (Stage 0 → Stage 1) on real data.

Pulls OKX BTC-USDT-SWAP OHLCV from the local TimescaleDB, computes the
helivex_oskill per-bar features on a rolling window (matching the canonical
window sizes in helivex_regime FEATURE_WINDOW_META / the oskill duckdb
integration test), feeds them through helivex_regime.RegimeClassifier, and
writes a joined parquet that the v5-1 backtest driver consumes.

Output columns (one row per bar that has a full feature window):
    bar_close_ts, open, high, low, close, log_return,
    realized_var, tail_index, hurst_h2, hurst_r2, delta_alpha,
    regime, risk_multiplier, funding_rate

Note: the guide says "Binance BTCUSDT.P"; the local DB holds OKX
BTC-USDT-SWAP (5m, 2019→2026), which covers the same crash windows.
--symbol BTCUSDT.P is accepted and mapped to BTC-USDT-SWAP.

Usage:
    python -m helivex.scripts.run_features \
        --symbol BTCUSDT.P --timeframe 1h \
        --start 2022-09-15 --end 2022-12-14 \
        --out ~/projects/helivex/data/smoke_1h.parquet
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
from pathlib import Path

import asyncpg
import numpy as np
import pandas as pd

from helivex_oskill.returns import log_returns
from helivex_oskill.volatility import realized_variance
from helivex_oskill.multifractal import mfdfa
from helivex_oskill.tails import hill_tail_index
from helivex_regime import FeatureBar, RegimeClassifier, RegimeConfig
from helivex_regime.config import FEATURE_WINDOW_META

DB_DSN = "postgresql://helios:helios_dev_pass@localhost:5434/helivex"
SRC_5M = "okx_swap_5m"  # clean, complete 5m series; resampled to 1h here

SYMBOL_MAP = {
    "BTCUSDT.P": "BTC-USDT-SWAP",
    "BTC-USDT-SWAP": "BTC-USDT-SWAP",
    "BTCUSDT": "BTC-USDT-SWAP",
}

# Canonical 1h windows (helivex_regime.config.FEATURE_WINDOW_META + oskill duckdb test)
HURST_WINDOW = FEATURE_WINDOW_META["hurst_window_1h"]  # 500
RV_WINDOW = 24
TAIL_FRACTION = 0.05

# Real funding (Binance single-exchange) — see ops/scripts/fetch_binance_funding.py
FUNDING_SYMBOL = "BTCUSDT"        # Binance perp funding key (≠ OKX BTC-USDT-SWAP)
FUNDING_SOURCE = "binance_rest"   # single-exchange; NOT the Coinalyze aggregate
SETTLE_HOURS = (0, 8, 16)         # 8h funding settlement instants (UTC)


# ──────────────────── data ────────────────────

async def _fetch_5m(instrument: str, start: datetime, end: datetime) -> list:
    conn = await asyncpg.connect(DB_DSN)
    try:
        rows = await conn.fetch(
            """SELECT bar_close_ts,
                      open::float, high::float, low::float, close::float, volume::float
               FROM market_data.ohlcv_1h
               WHERE instrument = $1 AND source = $2
                 AND bar_close_ts >= $3 AND bar_close_ts < $4
               ORDER BY bar_close_ts""",
            instrument, SRC_5M, start, end,
        )
    finally:
        await conn.close()
    if not rows:
        raise ValueError(
            f"No {SRC_5M} data for {instrument!r} in [{start}, {end})"
        )
    return rows


def _resample_1h(rows: list) -> pd.DataFrame:
    df = pd.DataFrame(
        rows, columns=["ts", "open", "high", "low", "close", "volume"]
    )
    df["ts"] = pd.to_datetime(df["ts"], utc=True)
    df = df.set_index("ts").sort_index()
    h = (
        df.resample("1h")
        .agg(
            open=("open", "first"),
            high=("high", "max"),
            low=("low", "min"),
            close=("close", "last"),
            volume=("volume", "sum"),
        )
        .dropna()
    )
    return h.reset_index()


# ──────────────────── features ────────────────────

def _compute_feature_rows(h: pd.DataFrame) -> pd.DataFrame:
    """Rolling Stage-0 features + Stage-1 regime, one row per bar with a full window."""
    ts = list(h["ts"])
    o = h["open"].to_numpy(np.float64)
    hi = h["high"].to_numpy(np.float64)
    lo = h["low"].to_numpy(np.float64)
    close = h["close"].to_numpy(np.float64)
    n = len(close)
    if n <= HURST_WINDOW + 1:
        raise ValueError(
            f"Need > {HURST_WINDOW + 1} 1h bars for a full Hurst window; got {n}. "
            "Widen --start/--end."
        )

    # r[j] = log(close[j+1]/close[j]); the return realized AT bar i is r[i-1].
    r = log_returns(close)

    feat_bars: list[FeatureBar] = []
    recs: list[dict] = []
    last_delta_alpha = 0.0

    for i in range(HURST_WINDOW, n):
        win = r[i - HURST_WINDOW:i]                # 500 returns ending at bar i
        mf = mfdfa(win)
        da = mf.delta_alpha
        if not np.isfinite(da):
            da = last_delta_alpha                  # forward-fill degenerate windows
        else:
            last_delta_alpha = da
        rv = realized_variance(r[i - RV_WINDOW:i])
        tail = hill_tail_index(np.abs(win), tail_fraction=TAIL_FRACTION)[0]

        feat_bars.append(
            FeatureBar(
                bar_close_ts=ts[i],
                hurst_h2=mf.h2,
                hurst_r2=mf.r_squared_h2,
                delta_alpha=da,
                realized_var=rv,
                tail_index=tail,
            )
        )
        recs.append(
            {
                "bar_close_ts": ts[i],
                "open": float(o[i]),
                "high": float(hi[i]),
                "low": float(lo[i]),
                "close": float(close[i]),
                "log_return": float(r[i - 1]),
                "realized_var": float(rv),
                "tail_index": float(tail),
                "hurst_h2": float(mf.h2),
                "hurst_r2": float(mf.r_squared_h2),
                "delta_alpha": float(da),
            }
        )

    # Stage 1 — regime classification over the feature stream
    clf = RegimeClassifier(RegimeConfig.for_timeframe("1h"))
    decisions = clf.process_stream(feat_bars)
    for rec, dec in zip(recs, decisions):
        rec["regime"] = dec.regime.value
        rec["risk_multiplier"] = float(dec.risk_multiplier)
        rec["funding_rate"] = 0.0  # default; overwritten at settlement bars by _join_funding

    return pd.DataFrame(recs)


# ──────────────────── funding (Piece A) ────────────────────

async def _fetch_funding(symbol: str, start: datetime, end: datetime) -> list:
    conn = await asyncpg.connect(DB_DSN)
    try:
        return await conn.fetch(
            """SELECT funding_time, funding_rate
               FROM market_data.binance_funding_history
               WHERE symbol = $1 AND funding_time >= $2 AND funding_time < $3
               ORDER BY funding_time""",
            symbol, start, end,
        )
    finally:
        await conn.close()


def _hour_key(ts: datetime) -> str:
    """UTC hour bucket key. Both sides are tz-aware UTC, so keys align exactly."""
    return ts.astimezone(timezone.utc).strftime("%Y-%m-%dT%H")


def _join_funding(out: pd.DataFrame, funding_rows: list) -> tuple[pd.DataFrame, int, int]:
    """Stamp real funding ONLY on settlement bars (0/8/16 UTC); 0.0 elsewhere.

    CRITICAL: the v5-1 simulator accrues position_fraction*funding_rate on EVERY
    bar, so non-settlement bars MUST be 0.0 — otherwise 1h funding is 3x-counted
    (24 bars/day, 3 settlements). Missing settlement rows → 0.0 + explicit warning.
    """
    fmap = {_hour_key(r["funding_time"]): float(r["funding_rate"]) for r in funding_rows}

    bar_ts = pd.to_datetime(out["bar_close_ts"], utc=True)
    rates: list[float] = []
    n_funded = 0
    missing: list[str] = []
    for ts in bar_ts:
        pyts = ts.to_pydatetime()
        is_settlement = (pyts.hour in SETTLE_HOURS) and (pyts.minute == 0)
        if not is_settlement:
            rates.append(0.0)
            continue
        rate = fmap.get(_hour_key(pyts))
        if rate is None:
            rates.append(0.0)
            missing.append(pyts.strftime("%Y-%m-%d %H:%M"))
        else:
            rates.append(rate)
            n_funded += 1

    out = out.copy()
    out["funding_rate"] = rates
    if missing:
        print(f"  WARNING: {len(missing)} settlement bars had NO funding row → filled 0.0 "
              f"(gap in binance_funding_history). First few: {missing[:5]}")
    return out, n_funded, len(missing)


def _write_parquet(out: pd.DataFrame, out_path: Path) -> None:
    """Write parquet with file-level provenance metadata (funding source)."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    table = pa.Table.from_pandas(out, preserve_index=False)
    md = dict(table.schema.metadata or {})
    md[b"funding_source"] = FUNDING_SOURCE.encode()
    md[b"funding_scope"] = b"single_exchange_binance"
    md[b"funding_symbol"] = FUNDING_SYMBOL.encode()
    table = table.replace_schema_metadata(md)
    pq.write_table(table, out_path)


# ──────────────────── eyeball checks ────────────────────

def _sanity_report(out: pd.DataFrame) -> None:
    h2 = out["hurst_h2"].to_numpy()
    da = out["delta_alpha"].to_numpy()
    print("\n── Step 2 sanity (behaviour, not value-correctness) ──")
    print(
        f"  hurst_h2: mean={np.nanmean(h2):.3f} std={np.nanstd(h2):.3f} "
        f"min={np.nanmin(h2):.3f} max={np.nanmax(h2):.3f} "
        f"nan_frac={np.mean(~np.isfinite(h2)):.3f}  "
        f"(want: wobbles ~0.5, not constant, not all-NaN)"
    )

    out = out.copy()
    out["d"] = pd.to_datetime(out["bar_close_ts"], utc=True)
    crash = out[(out["d"] >= "2022-11-07") & (out["d"] < "2022-11-11")]
    base = out[(out["d"] >= "2022-10-01") & (out["d"] < "2022-11-01")]
    if len(crash) and len(base):
        print(
            f"  delta_alpha: FTX 11/07-11/10 median={crash['delta_alpha'].median():.3f} "
            f"vs Oct baseline median={base['delta_alpha'].median():.3f}  "
            f"(want: crash wider)"
        )
        print(
            f"  regime during FTX window: "
            f"{crash['regime'].value_counts().to_dict()}  "
            f"min risk_multiplier={crash['risk_multiplier'].min():.3f}  "
            f"(want: UNSTABLE appears, risk_mult collapses ~0.1)"
        )
    print(f"  regime distribution (full): {out['regime'].value_counts().to_dict()}")
    print(
        f"  risk_multiplier: mean={out['risk_multiplier'].mean():.3f} "
        f"min={out['risk_multiplier'].min():.3f} max={out['risk_multiplier'].max():.3f}"
    )


# ──────────────────── main ────────────────────

def _parse_dt(s: str) -> datetime:
    return datetime.fromisoformat(s).replace(tzinfo=timezone.utc)


async def _run(args) -> None:
    instrument = SYMBOL_MAP.get(args.symbol, args.symbol)
    if args.timeframe != "1h":
        raise ValueError("only --timeframe 1h is wired in this smoke pipeline")
    start, end = _parse_dt(args.start), _parse_dt(args.end)
    print(f"Fetching {SRC_5M} {instrument} [{start.date()} → {end.date()}) …")
    rows = await _fetch_5m(instrument, start, end)
    h = _resample_1h(rows)
    print(f"  {len(rows)} 5m bars → {len(h)} 1h bars")
    print(f"Computing oskill features (window={HURST_WINDOW}) + regime …")
    out = _compute_feature_rows(h)
    print(f"  {len(out)} feature rows (first {HURST_WINDOW} bars consumed as warmup)")

    # Piece A: join real Binance funding, settlement-bars only (0/8/16 UTC)
    funding_rows = await _fetch_funding(FUNDING_SYMBOL, start, end)
    out, n_funded, n_missing = _join_funding(out, funding_rows)
    nonzero = int((out["funding_rate"] != 0.0).sum())
    print(f"  funding: {len(funding_rows)} settlements in window; "
          f"{n_funded} stamped onto settlement bars, {n_missing} missing→0.0; "
          f"{nonzero} bars carry nonzero funding")

    out_path = Path(args.out).expanduser()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    _write_parquet(out, out_path)
    print(f"Wrote {out_path}  (funding_source={FUNDING_SOURCE})")
    _sanity_report(out)


def main() -> None:
    p = argparse.ArgumentParser(description="Step 2: features + regime → joined parquet")
    p.add_argument("--symbol", default="BTCUSDT.P")
    p.add_argument("--timeframe", default="1h")
    p.add_argument("--start", required=True, help="ISO date, UTC, e.g. 2022-09-15")
    p.add_argument("--end", required=True, help="ISO date, UTC (exclusive)")
    p.add_argument("--out", required=True)
    args = p.parse_args()
    asyncio.run(_run(args))


if __name__ == "__main__":
    main()
