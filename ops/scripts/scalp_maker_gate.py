#!/usr/bin/env python3
"""Scalp maker-v2 — 3-dimension redesign of scalp_5m, gated honestly.

Context
-------
scalp_5m (R5) was NO-GO: gross Sharpe +1.33 but cost-killed — paper run confirmed
100% TAKER fills (5bps/side) and a fixed time_exit. Maker feasibility is now
verified live on OKX (post_only rests as maker, refuses to cross; fee Lv1 =
maker 2bps vs taker 5bps). This script redesigns scalp along three dimensions
and asks the *honest* question: does it pass the helivex gate under REAL maker
cost and a CONSERVATIVE fill rate?

Three dimensions (all params A-PRIORI, fixed once — NO tuning-to-pass)
---------------------------------------------------------------------
D1 execution = maker:
   - entries are post_only limit (maker, 2bps); modelled with a conservative
     fill rate (default 0.75) — missed entries simply don't trade.
   - profit-target exits are maker (2bps); stop / trailing / timeout exits are
     TAKER (5bps, they must execute).
D2 entry = lower freq, higher quality:
   - z threshold raised 2.0 -> 2.5. NO confluence indicator added (deliberately
     minimal DoF; adding filters here is where overfitting creeps in).
D3 exit = asymmetric payoff + scale-out (the R5 death was fixed time_exit):
   - 50% (half A) takes profit at reversion to VWAP (z->0)         [maker]
   - 50% (half B) is a runner: target overshoot z<=-1.5 [maker], else trailing
     stop 15bps off the favorable extreme [taker]
   - hard stop: |z|>=4.0 (stretch expands against us) -> exit all  [taker]
   - timeout: 36 bars (3h) -> exit all                             [taker]

Anti-self-deception
-------------------
- Real maker/taker cost (2/5 bps), NOT R5's 2bps maker fantasy.
- Fill rate is the key unknown: verdict basis = conservative 0.75; we report
  0.70/0.80/0.90 sensitivity (sensitivity != selection — verdict is pre-committed
  at 0.75).
- Same helivex gate as all prior trials (walk-forward CPCV -> DSR, PBO, threshold
  adjusted for the GLOBAL trial count). This is the next global trial.
- If it still fails: scalp is genuinely NO-GO — but now we've exhausted the
  maker + redesign avenue, not just the taker version.

Run:  ./venv/bin/python ops/scripts/scalp_maker_gate.py [--register]
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

import asyncpg
import numpy as np
import pandas as pd

PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
from tools.strategy_gate import (  # noqa: E402
    _walk_forward_gate, _sharpe, _dsr_threshold, _load_trials, _save_trial,
)

DB_DSN      = "postgresql://helios:helios_dev_pass@localhost:5434/helivex"
SOURCE_5M   = "okx_swap_5m"
INSTRUMENTS = ["BTC-USDT-SWAP", "ETH-USDT-SWAP", "SOL-USDT-SWAP"]
PERIODS_5M  = 365 * 288        # 5m bars per year

# ── A-priori params (fixed once) ──────────────────────────────────────────────
VWAP_N      = 12               # 60min rolling VWAP window (R5)
Z_ENTRY     = 2.5              # D2: raised from 2.0
TP_Z        = 0.0             # D3: half A exits at reversion to VWAP
RUN_Z       = -1.5            # D3: runner overshoot target (beyond mean)
TRAIL_BPS   = 15.0            # D3: runner trailing stop, bps off favorable extreme
STOP_Z      = 4.0             # D3: hard stop if stretch expands against us
MAX_HOLD    = 36             # D3: timeout bars (3h)

MAKER_BPS   = 2.0             # OKX Lv1 maker (verified)
TAKER_BPS   = 5.0             # OKX Lv1 taker (verified)

FILL_RATES  = [0.70, 0.75, 0.80, 0.90]
VERDICT_FR  = 0.75            # pre-committed conservative verdict basis
SEED        = 42

# Gate params (identical to tools/strategy_gate defaults)
N_SPLITS, EMBARGO, PBO_THR = 6, 50, 0.5


async def load_5m(inst: str) -> pd.DataFrame:
    conn = await asyncpg.connect(DB_DSN)
    rows = await conn.fetch(
        """SELECT bar_close_ts, close::float, volume::float
           FROM market_data.ohlcv_1h WHERE instrument=$1 AND source=$2
           ORDER BY bar_close_ts""", inst, SOURCE_5M)
    await conn.close()
    df = pd.DataFrame([dict(r) for r in rows])
    return df


def zscore(closes: np.ndarray, volumes: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """VWAP z-score on prior VWAP_N bars (shift(1), no look-ahead)."""
    n = len(closes)
    vwap = np.full(n, np.nan)
    z = np.full(n, np.nan)
    for i in range(VWAP_N + 1, n):
        pc = closes[i - VWAP_N:i]      # prior VWAP_N closes (excludes bar i)
        pv = volumes[i - VWAP_N:i]
        vw = float((pc * pv).sum() / (pv.sum() + 1e-10))
        sd = float(pc.std(ddof=1))
        vwap[i] = vw
        z[i] = (closes[i] - vw) / (sd + 1e-10)
    return vwap, z


def simulate(closes: np.ndarray, vwap: np.ndarray, z: np.ndarray,
             fill_rate: float, seed: int) -> tuple[np.ndarray, dict]:
    """Maker-v2 bracket sim → per-bar net return series + trade stats.

    Causal: decisions at bar i use close[i], vwap[i], z[i] (all from info <= i).
    Position held over [i-1, i] earns bar return; fill costs charged at action bar.
    """
    n = len(closes)
    ret = np.zeros(n)
    rng = np.random.default_rng(seed)

    pos = 0.0           # signed size currently held (e.g. -1.0 short, -0.5 half)
    side = 0            # +1 long / -1 short
    a_open = b_open = False
    best = 0.0          # most-favorable price since entry (low for short, high for long)
    held = 0
    stats = dict(signals=0, entries=0, missed=0, trades=0, wins=0,
                 maker_fills=0, taker_fills=0, pnl_sum=0.0, rets=[])
    entry_px = 0.0

    def cost(bps: float, size: float) -> float:
        return bps / 1e4 * abs(size)

    for i in range(VWAP_N + 2, n):
        c, cp = closes[i], closes[i - 1]
        ret[i] = pos * (c - cp) / (cp + 1e-10)      # MTM on position held into bar i
        ci = 0.0

        if pos == 0.0:
            # entry decision
            if not np.isnan(z[i]) and abs(z[i]) >= Z_ENTRY:
                stats["signals"] += 1
                if rng.random() < fill_rate:            # maker entry filled?
                    side = -1 if z[i] >= Z_ENTRY else 1  # fade the stretch
                    pos = -1.0 if side == -1 else 1.0
                    a_open = b_open = True
                    entry_px = c
                    best = c
                    held = 0
                    ci += cost(MAKER_BPS, 1.0)           # maker entry, full size
                    stats["entries"] += 1; stats["maker_fills"] += 1
                else:
                    stats["missed"] += 1
        else:
            held += 1
            # update favorable extreme
            best = min(best, c) if side == -1 else max(best, c)
            zi = z[i]
            # adverse stretch (signed so + = against us)
            adverse_z = (zi if side == -1 else -zi)
            trail_hit = (c >= best * (1 + TRAIL_BPS/1e4)) if side == -1 else (c <= best * (1 - TRAIL_BPS/1e4))
            revert_z  = (zi <= TP_Z) if side == -1 else (zi >= -TP_Z)
            over_z    = (zi <= RUN_Z) if side == -1 else (zi >= -RUN_Z)

            exit_all_taker = (adverse_z >= STOP_Z) or (held >= MAX_HOLD)
            if exit_all_taker:
                sz = pos
                ci += cost(TAKER_BPS, sz); stats["taker_fills"] += 1
                pos = 0.0; a_open = b_open = False
            else:
                # half A: take profit at VWAP revert (maker)
                if a_open and revert_z:
                    sz = 0.5 * side
                    ci += cost(MAKER_BPS, 0.5); stats["maker_fills"] += 1
                    pos -= sz; a_open = False
                # half B: runner — overshoot target (maker) or trailing (taker)
                if b_open:
                    if over_z:
                        ci += cost(MAKER_BPS, 0.5); stats["maker_fills"] += 1
                        pos -= 0.5 * side; b_open = False
                    elif (not a_open) and trail_hit:    # trail only after A booked
                        ci += cost(TAKER_BPS, 0.5); stats["taker_fills"] += 1
                        pos -= 0.5 * side; b_open = False
            if pos == 0.0 and not a_open and not b_open:
                # trade closed this bar — record realized trade return (approx)
                stats["trades"] += 1

        ret[i] -= ci

    stats["rets"] = ret
    stats["fill_rate_real"] = stats["entries"] / stats["signals"] if stats["signals"] else 0.0
    return ret, stats


def run_instrument(df: pd.DataFrame, fill_rate: float, seed: int) -> tuple[np.ndarray, dict]:
    closes = df["close"].to_numpy(float)
    volumes = df["volume"].to_numpy(float)
    vwap, z = zscore(closes, volumes)
    ret, stats = simulate(closes, vwap, z, fill_rate, seed)
    return ret, stats


async def main(register: bool) -> None:
    print("=" * 78)
    print("Scalp maker-v2 gate — maker exec + z=2.5 + scale-out bracket")
    print("=" * 78)
    trial_n = _load_trials()["total_trials"] + 1
    dsr_thr = _dsr_threshold(trial_n)
    print(f"Global trial #{trial_n}   DSR bar: {dsr_thr:.3f}   "
          f"verdict fill-rate: {VERDICT_FR}   maker/taker cost: {MAKER_BPS}/{TAKER_BPS}bps")
    print(f"A-priori: z_entry={Z_ENTRY} TP@VWAP runnerZ={RUN_Z} trail={TRAIL_BPS}bps "
          f"stopZ={STOP_Z} maxhold={MAX_HOLD}\n")

    data = {inst: await load_5m(inst) for inst in INSTRUMENTS}
    for inst, df in data.items():
        print(f"  {inst}: {len(df):,} 5m bars")
    print()

    results = {}   # (inst, fr) -> gate
    for fr in FILL_RATES:
        results[fr] = {}
        for k, inst in enumerate(INSTRUMENTS):
            ret, st = run_instrument(data[inst], fr, SEED + k)
            gate = _walk_forward_gate(ret, N_SPLITS, EMBARGO, PERIODS_5M, PBO_THR)
            gate["gross"] = _sharpe(ret, PERIODS_5M)
            gate["adj_dsr"] = gate["deflated_sharpe"] - dsr_thr
            gate["stats"] = st
            results[fr][inst] = gate

    # detail table at verdict fill rate
    print(f"── Gate @ verdict fill_rate={VERDICT_FR} ──")
    print(f"  {'inst':14} {'sig':>6} {'trades':>6} {'mk/tk fills':>12} {'gross':>7} "
          f"{'meanOOS':>8} {'foldStd':>8} {'DSR':>7} {'adjDSR':>7} {'PBO':>5}  verdict")
    for inst in INSTRUMENTS:
        g = results[VERDICT_FR][inst]; s = g["stats"]
        fstd = float(np.std(g["oos_sharpes"])) if g["oos_sharpes"] else float("nan")
        status = "PASS" if (not g["fail_reasons"] and g["adj_dsr"] > 0) else "FAIL"
        g["status"] = status
        print(f"  {inst:14} {s['signals']:>6} {s['trades']:>6} "
              f"{s['maker_fills']:>5}/{s['taker_fills']:<6} {g['gross']:>7.3f} "
              f"{g['mean_oos_sharpe']:>8.3f} {fstd:>8.3f} {g['deflated_sharpe']:>7.3f} "
              f"{g['adj_dsr']:>7.3f} {g['pbo']:>5.2f}  {status}")
    overall = "PASS" if all(results[VERDICT_FR][i]["status"] == "PASS" for i in INSTRUMENTS) else "FAIL"

    # fill-rate sensitivity (mean DSR across instruments)
    print("\n── Fill-rate sensitivity (mean DSR across instruments) ──")
    for fr in FILL_RATES:
        dsrs = [results[fr][i]["deflated_sharpe"] for i in INSTRUMENTS]
        adjs = [results[fr][i]["adj_dsr"] for i in INSTRUMENTS]
        passes = sum(1 for i in INSTRUMENTS
                     if not results[fr][i]["fail_reasons"] and results[fr][i]["adj_dsr"] > 0)
        tag = "  <- verdict basis" if fr == VERDICT_FR else ""
        print(f"  fill_rate={fr:.2f}: meanDSR={np.mean(dsrs):+.3f}  meanAdjDSR={np.mean(adjs):+.3f}  "
              f"pass {passes}/3{tag}")

    # cost-attribution sanity: realized maker/taker mix at verdict fr
    tot_mk = sum(results[VERDICT_FR][i]["stats"]["maker_fills"] for i in INSTRUMENTS)
    tot_tk = sum(results[VERDICT_FR][i]["stats"]["taker_fills"] for i in INSTRUMENTS)
    print(f"\nFill mix @ {VERDICT_FR}: maker={tot_mk} taker={tot_tk} "
          f"(taker share {tot_tk/(tot_mk+tot_tk+1e-9)*100:.0f}% — stops/timeouts/trails)")

    print("\n" + "=" * 78)
    print(f"SCALP MAKER-V2 OVERALL VERDICT (@fr={VERDICT_FR}, N={trial_n}): {overall}")
    print("=" * 78)

    if register:
        metrics = {"instruments": {inst: {
            "status": results[VERDICT_FR][inst]["status"],
            "dsr": results[VERDICT_FR][inst]["deflated_sharpe"],
            "pbo": results[VERDICT_FR][inst]["pbo"],
            "mean_oos": results[VERDICT_FR][inst]["mean_oos_sharpe"],
            "gross_sharpe": results[VERDICT_FR][inst]["gross"],
        } for inst in INSTRUMENTS}, "overall": overall,
            "note": f"scalp maker-v2 (z=2.5, scale-out bracket, maker {MAKER_BPS}bps, fill_rate {VERDICT_FR})"}
        tn = _save_trial("ops/scripts/scalp_maker_gate.py (scalp maker-v2)", overall, metrics)
        print(f"\nRegistered as global trial #{tn}")
    else:
        print("\n(dry run — not registered; pass --register)")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--register", action="store_true")
    args = ap.parse_args()
    asyncio.run(main(args.register))
