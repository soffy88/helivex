"""R3.1: backfill expanded universe, batch cointegration screen, stat_arb gate per pair.

Steps:
    1. Backfill 1H OHLCV for new instruments (BCH/LTC/ETC/XRP/DOGE/ADA-USDT).
    2. Load all instruments from DB, align timestamps.
    3. Engle-Granger cointegration test on all C(N,2) pairs.
    4. For each pair with p < 0.05: run stat_arb backtest_gate.
    5. Write docs/R3.1_STAT_ARB_PAIRS.md with full results.

No OKX API key required.

Usage:
    python ops/scripts/pair_screener.py [--months 12] [--no-backfill] [--quiet]
"""
from __future__ import annotations

import argparse
import asyncio
import itertools
import sys
import time
from pathlib import Path

import asyncpg
import httpx
import numpy as np

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

DB_DSN = "postgresql://helios:helios_dev_pass@localhost:5434/helivex"
OKX_BASE = "https://www.okx.com"

EXISTING = ["BTC-USDT", "ETH-USDT", "SOL-USDT"]
NEW_INSTRUMENTS = ["BCH-USDT", "LTC-USDT", "ETC-USDT", "XRP-USDT", "DOGE-USDT", "ADA-USDT"]
ALL_INSTRUMENTS = EXISTING + NEW_INSTRUMENTS

# OKX perp taker 0.02% per leg × 2 legs × 2 sides (open + close)
STAT_ARB_ROUND_TRIP = 0.0002 * 4  # 0.0008 = 8bps
STAT_ARB_PERIODS = 24 * 252       # 1H bars, annualised to ~252 trading days


# ── Backfill ──────────────────────────────────────────────────────────────────
async def _fetch_candles(
    client: httpx.AsyncClient,
    inst_id: str,
    bar: str,
    after_ms: int | None,
    limit: int = 100,
) -> list[list]:
    from datetime import datetime, timedelta, timezone  # noqa: PLC0415

    params: dict = {"instId": inst_id, "bar": bar, "limit": str(limit)}
    if after_ms is not None:
        params["after"] = str(after_ms)
    for endpoint in ["/api/v5/market/history-candles", "/api/v5/market/candles"]:
        try:
            r = await client.get(OKX_BASE + endpoint, params=params, timeout=15)
            data = r.json()
            if str(data.get("code", "1")) == "0":
                return data.get("data", [])
        except Exception:
            pass
    return []


async def _backfill_instrument(
    conn: asyncpg.Connection,
    client: httpx.AsyncClient,
    inst_id: str,
    months: int,
) -> int:
    from datetime import datetime, timedelta, timezone  # noqa: PLC0415

    cutoff_ms = int(
        (datetime.now(timezone.utc) - timedelta(days=30 * months)).timestamp() * 1000
    )
    rows_inserted = 0
    after_ms: int | None = None

    while True:
        candles = await _fetch_candles(client, inst_id, "1H", after_ms)
        if not candles:
            break
        oldest_ts = int(candles[-1][0])
        records = []
        for row in candles:
            ts_ms = int(row[0])
            if ts_ms < cutoff_ms:
                continue
            from datetime import datetime, timezone  # noqa: PLC0415

            bar_close_ts = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)
            records.append((
                inst_id, bar_close_ts, "okx",
                float(row[1]), float(row[2]), float(row[3]), float(row[4]),
                float(row[5]),
                float(row[6]) if len(row) > 6 else None,
            ))
        if records:
            result = await conn.executemany(
                """INSERT INTO market_data.ohlcv_1h
                   (instrument, bar_close_ts, source, open, high, low, close, volume, quote_volume)
                   VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
                   ON CONFLICT (instrument, bar_close_ts, source) DO NOTHING""",
                records,
            )
            rows_inserted += int(result.split()[-1]) if result else 0
        if oldest_ts <= cutoff_ms:
            break
        after_ms = oldest_ts
        await asyncio.sleep(0.12)

    return rows_inserted


async def run_backfill(months: int, quiet: bool) -> dict[str, int]:
    conn = await asyncpg.connect(DB_DSN)
    counts: dict[str, int] = {}
    headers = {"User-Agent": "helivex-pair-screener/1.0"}
    async with httpx.AsyncClient(headers=headers) as client:
        for inst in NEW_INSTRUMENTS:
            t0 = time.monotonic()
            n = await _backfill_instrument(conn, client, inst, months)
            counts[inst] = n
            # Count actual rows in DB
            db_count = await conn.fetchval(
                "SELECT COUNT(*) FROM market_data.ohlcv_1h WHERE instrument = $1", inst
            )
            if not quiet:
                print(f"  {inst:12s}  +{n:5d} rows  (DB total: {db_count})  ({time.monotonic()-t0:.1f}s)")
    await conn.close()
    return counts


# ── Data loading ──────────────────────────────────────────────────────────────
async def _load_all_instruments() -> dict[str, list[tuple]]:
    """Return {instrument: [(bar_close_ts, close), ...]} sorted ascending."""
    conn = await asyncpg.connect(DB_DSN)
    result: dict[str, list[tuple]] = {}
    for inst in ALL_INSTRUMENTS:
        rows = await conn.fetch(
            "SELECT bar_close_ts, close FROM market_data.ohlcv_1h "
            "WHERE instrument = $1 ORDER BY bar_close_ts ASC",
            inst,
        )
        result[inst] = [(r["bar_close_ts"], float(r["close"])) for r in rows]
    await conn.close()
    return result


def align_pair(
    ts_a: list[tuple], ts_b: list[tuple]
) -> tuple[np.ndarray, np.ndarray]:
    """Align two series on timestamps, return (prices_a, prices_b)."""
    map_a = {r[0]: r[1] for r in ts_a}
    map_b = {r[0]: r[1] for r in ts_b}
    common = sorted(set(map_a) & set(map_b))
    a = np.array([map_a[t] for t in common])
    b = np.array([map_b[t] for t in common])
    return a, b


# ── Cointegration screening ───────────────────────────────────────────────────
def screen_cointegration(
    series_map: dict[str, list[tuple]],
    quiet: bool,
) -> list[dict]:
    """Run Engle-Granger on all C(N,2) pairs. Return sorted list of results."""
    from oprim.cointegration_test import cointegration_test  # noqa: PLC0415

    instruments = list(series_map.keys())
    pairs = list(itertools.combinations(instruments, 2))
    results = []

    if not quiet:
        print(f"  Screening {len(pairs)} pairs ({len(instruments)} instruments)...")

    for a_name, b_name in pairs:
        a, b = align_pair(series_map[a_name], series_map[b_name])
        if len(a) < 100:
            continue
        try:
            coint = cointegration_test(a, b)
            results.append({
                "pair": (a_name, b_name),
                "p_value": float(coint["p_value"]),
                "cointegrated": bool(coint["cointegrated"]),
                "hedge_ratio": float(coint["hedge_ratio"]),
                "n_bars": len(a),
            })
        except Exception as exc:
            results.append({
                "pair": (a_name, b_name),
                "p_value": 1.0,
                "cointegrated": False,
                "hedge_ratio": 0.0,
                "n_bars": len(a),
                "error": str(exc),
            })

    results.sort(key=lambda r: r["p_value"])
    return results


# ── Stat-arb strategy & gate ──────────────────────────────────────────────────
def _spread_returns(
    a: np.ndarray,
    b: np.ndarray,
    hedge: float,
    mean_s: float,
    std_s: float,
    ref: float,
    entry_z: float = 2.0,
    exit_z: float = 0.5,
) -> list[float]:
    """Per-bar % returns for a mean-reversion strategy on the spread b − hedge·a."""
    spread = b - hedge * a
    z = (spread - mean_s) / (std_s + 1e-12)
    pos = 0
    rets: list[float] = []

    for i in range(1, len(spread)):
        dspread = float(spread[i] - spread[i - 1])
        bar_ret = pos * dspread / ref  # % of reference price
        zi_prev = float(z[i - 1])     # signal from previous close (no look-ahead)

        if pos == 0:
            if zi_prev <= -entry_z:
                pos = 1
                bar_ret = pos * dspread / ref - STAT_ARB_ROUND_TRIP
            elif zi_prev >= entry_z:
                pos = -1
                bar_ret = pos * dspread / ref - STAT_ARB_ROUND_TRIP
        else:
            if abs(zi_prev) <= exit_z:
                pos = 0   # exit; round-trip already charged at entry

        rets.append(bar_ret)
    return rets


def _compute_sharpe(returns: list[float], periods: int) -> float:
    arr = np.asarray(returns, dtype=float)
    if len(arr) < 2:
        return 0.0
    std = float(arr.std())
    return 0.0 if std < 1e-12 else float((arr.mean() / std) * np.sqrt(periods))


def make_strategy_fn(pair_data: list[tuple[float, float]]):
    def fn(train_data: list, test_data: list) -> dict:
        if len(train_data) < 60 or len(test_data) < 5:
            return {"sharpe": 0.0, "returns": [], "is_sharpe": 0.0}

        a_tr = np.array([d[0] for d in train_data])
        b_tr = np.array([d[1] for d in train_data])

        # OLS hedge ratio from training
        X = np.column_stack([a_tr, np.ones(len(a_tr))])
        hedge = float(np.linalg.lstsq(X, b_tr, rcond=None)[0][0])

        spread_tr = b_tr - hedge * a_tr
        mean_s = float(spread_tr.mean())
        std_s = float(spread_tr.std())
        ref = float(np.mean(np.abs(b_tr))) + 1e-8

        is_rets = _spread_returns(a_tr, b_tr, hedge, mean_s, std_s, ref)
        is_sr = _compute_sharpe(is_rets, STAT_ARB_PERIODS)

        a_te = np.array([d[0] for d in test_data])
        b_te = np.array([d[1] for d in test_data])
        oos_rets = _spread_returns(a_te, b_te, hedge, mean_s, std_s, ref)
        oos_sr = _compute_sharpe(oos_rets, STAT_ARB_PERIODS)

        return {"sharpe": oos_sr, "returns": oos_rets, "is_sharpe": is_sr}

    return fn


def run_gate_for_pair(
    a_name: str,
    b_name: str,
    a: np.ndarray,
    b: np.ndarray,
) -> dict:
    from omodul.backtest_gate import BacktestGateConfig, backtest_gate  # noqa: PLC0415

    pair_data = list(zip(a.tolist(), b.tolist()))
    strategy_fn = make_strategy_fn(pair_data)

    config = BacktestGateConfig(
        strategy_name=f"stat_arb_{a_name}_{b_name}",
        n_splits=5,
        embargo=24,
        pbo_threshold=0.5,
        periods=STAT_ARB_PERIODS,
    )
    gate = backtest_gate(strategy_fn, pair_data, config=config)

    wf = gate["walk_forward_result"]
    is_sharpes = [fr.get("is_sharpe", fr.get("sharpe", 0.0)) for fr in wf["fold_results"]]
    mean_is = float(np.mean(is_sharpes)) if is_sharpes else 0.0

    return {
        "pair": f"{a_name}/{b_name}",
        "gate_status": gate["gate_status"],
        "mean_is_sharpe": round(mean_is, 4),
        "mean_oos_sharpe": round(gate["mean_oos_sharpe"], 4),
        "deflated_sharpe": round(gate["deflated_sharpe"], 4),
        "pbo": round(gate["pbo"], 4),
        "fail_reasons": gate["fail_reasons"],
    }


# ── Verdict doc ───────────────────────────────────────────────────────────────
def write_verdict(
    coint_results: list[dict],
    gate_results: list[dict],
) -> None:
    docs = Path(__file__).parent.parent.parent / "docs"
    docs.mkdir(exist_ok=True)

    cointed = [r for r in coint_results if r["cointegrated"]]
    passed_gates = [g for g in gate_results if g["gate_status"] == "passed"]

    lines = [
        "# R3.1 Stat-Arb Pair Screening",
        "",
        f"> Universe: {', '.join(ALL_INSTRUMENTS)}  "
        f"({len(coint_results)} pairs screened)",
        "",
        "## Cointegration Screening (Engle-Granger, p < 0.05)",
        "",
        f"**{len(cointed)} cointegrated pairs** out of {len(coint_results)} tested.",
        "",
        "| Pair (A / B) | p-value | Cointegrated | Hedge Ratio | Bars |",
        "|---|---|---|---|---|",
    ]
    for r in coint_results:
        a, b = r["pair"]
        coint_mark = "**YES**" if r["cointegrated"] else "no"
        lines.append(
            f"| {a} / {b} | {r['p_value']:.4f} | {coint_mark} | {r['hedge_ratio']:.4f} | {r['n_bars']} |"
        )

    lines += [
        "",
        "## Backtest Gate Results (cointegrated pairs only)",
        "",
        f"**{len(passed_gates)} of {len(gate_results)} cointegrated pairs pass the gate.**",
        "",
        "| Pair | IS Sharpe | OOS Sharpe | DSR | PBO | Gate |",
        "|---|---|---|---|---|---|",
    ]
    for g in gate_results:
        icon = "**PASS**" if g["gate_status"] == "passed" else "FAIL"
        lines.append(
            f"| {g['pair']} | {g['mean_is_sharpe']:.4f} | {g['mean_oos_sharpe']:.4f} "
            f"| {g['deflated_sharpe']:.4f} | {g['pbo']:.4f} | {icon} |"
        )

    if not gate_results:
        lines += ["", "*(No cointegrated pairs found — no gates run.)*"]

    near_pass = [g for g in gate_results
                 if g["gate_status"] != "passed" and g["deflated_sharpe"] > 0]

    lines += ["", "## Honest Conclusion", ""]
    if passed_gates:
        lines += [
            f"**{len(passed_gates)} pair(s) pass the gate — alpha candidate(s) found.**",
            "",
            "Pairs with positive OOS DSR after walk-forward + multiple-testing correction:",
        ]
        for g in passed_gates:
            lines.append(f"- {g['pair']}: OOS SR={g['mean_oos_sharpe']:.4f}, DSR={g['deflated_sharpe']:.4f}")
    elif cointed:
        lines += [
            "**All cointegrated pairs fail the combined DSR + PBO gate.**",
            "",
        ]
        if near_pass:
            lines += [
                "**Near-pass (DSR > 0 but PBO > 0.5):**",
                "",
            ]
            for g in near_pass:
                lines += [
                    f"- **{g['pair']}**: OOS SR={g['mean_oos_sharpe']:.4f}, "
                    f"DSR=+{g['deflated_sharpe']:.4f}, PBO={g['pbo']:.4f}",
                    "  - DSR is positive (survives multiple-testing correction) but the",
                    "    IS-optimal fold underperforms the OOS median (PBO > 0.5).",
                    "  - Interpretation: the alpha signal exists but IS selection picks the",
                    "    wrong fold — consistent with a weak or regime-dependent edge.",
                    "",
            ]
        lines += [
            "Stat-arb has no walk-forward alpha on this OKX alt-coin universe at 1H",
            "resolution after 8bps round-trip cost. Possible causes: regime change,",
            "thin spread relative to fees, or short effective cointegration windows.",
        ]
    else:
        lines += [
            "**No cointegrated pairs found. Stat-arb has no theoretical basis in this universe.**",
        ]

    lines += [
        "",
        "---",
        "*Generated by `ops/scripts/pair_screener.py` — R3.1 milestone.*",
        "*Data: market_data.ohlcv_1h (OKX public API, 12-month 1H bars).*",
    ]

    (docs / "R3.1_STAT_ARB_PAIRS.md").write_text("\n".join(lines) + "\n")


# ── Main ──────────────────────────────────────────────────────────────────────
def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--months", type=int, default=12)
    parser.add_argument("--no-backfill", action="store_true")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()
    quiet = args.quiet

    # 1. Backfill new instruments
    if not args.no_backfill:
        if not quiet:
            print(f"=== Backfilling new instruments (1H, {args.months} months) ===")
        asyncio.run(run_backfill(args.months, quiet))

    # 2. Load all instruments
    if not quiet:
        print("\n=== Loading instruments from DB ===")
    series_map = asyncio.run(_load_all_instruments())
    for inst, rows in series_map.items():
        if not quiet:
            print(f"  {inst:12s}  {len(rows):5d} bars")

    # 3. Cointegration screening
    if not quiet:
        print("\n=== Cointegration screening ===")
    coint_results = screen_cointegration(series_map, quiet)
    cointed = [r for r in coint_results if r["cointegrated"]]

    if not quiet:
        for r in coint_results:
            a, b = r["pair"]
            flag = "<-- p<0.05 COINTEGRATED" if r["cointegrated"] else ""
            print(f"  {a:12s} / {b:12s}  p={r['p_value']:.4f}  hedge={r['hedge_ratio']:+.4f}  {flag}")

    print(f"\nCointegration summary: {len(cointed)}/{len(coint_results)} pairs p<0.05")
    for r in cointed:
        a, b = r["pair"]
        print(f"  {a} / {b}  p={r['p_value']:.4f}")

    # 4. Backtest gate for each cointegrated pair
    gate_results = []
    if cointed:
        if not quiet:
            print(f"\n=== Running backtest_gate for {len(cointed)} cointegrated pair(s) ===")
        for r in cointed:
            a_name, b_name = r["pair"]
            a, b = align_pair(series_map[a_name], series_map[b_name])
            t0 = time.monotonic()
            gate = run_gate_for_pair(a_name, b_name, a, b)
            gate_results.append(gate)
            elapsed = time.monotonic() - t0
            print(
                f"  {a_name}/{b_name:12s}  IS={gate['mean_is_sharpe']:+.4f}  "
                f"OOS={gate['mean_oos_sharpe']:+.4f}  DSR={gate['deflated_sharpe']:+.4f}  "
                f"PBO={gate['pbo']:.4f}  → {gate['gate_status'].upper()}  ({elapsed:.1f}s)"
            )
    else:
        if not quiet:
            print("\nNo cointegrated pairs — skipping gate.")

    # 5. Write verdict
    write_verdict(coint_results, gate_results)
    if not quiet:
        print("\ndocs/R3.1_STAT_ARB_PAIRS.md written")

    # 6. Key:value summary for determinism
    passed = [g for g in gate_results if g["gate_status"] == "passed"]
    print(f"\n  n_instruments        : {len(ALL_INSTRUMENTS)}")
    print(f"  n_pairs_screened     : {len(coint_results)}")
    print(f"  n_cointegrated       : {len(cointed)}")
    print(f"  n_passed_gate        : {len(passed)}")
    for g in gate_results:
        key = g["pair"].replace("/", "_vs_").replace("-USDT", "")
        print(f"  {key:32s}: {g['gate_status']}  OOS={g['mean_oos_sharpe']}  DSR={g['deflated_sharpe']}")


if __name__ == "__main__":
    main()
