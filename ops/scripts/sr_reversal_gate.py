#!/usr/bin/env python3
"""Price-action Support/Resistance reversal — ETH, low-freq, rule-based, gated.

Formalization of a well-known discretionary method (S/R reversal with candlestick
confirmation, the kind documented on trading wikis / by price-action educators).
Goal: test whether a *mechanical* approximation of that method has real,
gate-surviving edge on ETH — before any leverage.

Rules (long; short is symmetric at resistance), all params A-PRIORI
-------------------------------------------------------------------
Zones (higher-TF, multi-touch):
  - Daily pivot lows (low < lows of ±PIVOT_K days), CONFIRMED only at day d+K
    (no look-ahead). Higher-TF = daily.
  - A support ZONE = a price where >=MIN_TOUCH (2) confirmed daily pivot lows
    cluster within +-ZONE_BUF (0.3%). Zone band = mean*(1 +- ZONE_BUF).
  - Trading on 4h bars uses only zones whose touches were confirmed before the
    current day (causal).
Entry (4h):
  - Price taps the support band (zone_lo <= bar low <= zone_hi).
  - Reversal confirm on that bar: PIN/hammer (lower wick >= 2x body AND close in
    upper half) OR bullish ENGULFING (bull bar engulfs prior bear bar).
  - Enter next bar (~confirm close). Maker post_only.
Filter (don't catch the falling knife):
  - Skip longs in a strong daily downtrend: daily close < daily MA200 AND MA200
    falling (vs 20d ago). Symmetric for shorts.
Exit:
  - Stop: below the rejection bar low (low*(1-STOP_BUF), absorbs the wick).
  - Scale-out: half at +1R (1:1 lock), half at +3R (3:1, BTC price-action
    standard).  R = entry - stop.  (Fixed 3R chosen over "next resistance" to
    minimise DoF — anti-p-hacking.)
  - Stop = taker (must exit, 5bps); targets = maker (2bps).
Execution: maker post_only (verified feasible on OKX SWAP). Leverage: 1x only —
edge first, leverage never before edge is confirmed.

Anti-self-deception: params fixed once (no tune-to-pass); real maker/taker cost;
same helivex gate (walk-forward CPCV -> DSR, PBO, global-trial-adjusted). New
global trial. ETH only (as specified).

Run:  ./venv/bin/python ops/scripts/sr_reversal_gate.py [--register]
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

DB_DSN     = "postgresql://helios:helios_dev_pass@localhost:5434/helivex"
SOURCE_4H  = "okx_swap"
INSTRUMENT = "ETH-USDT-SWAP"
PERIODS_4H = 365 * 6

# ── A-priori params (fixed once) ──────────────────────────────────────────────
PIVOT_K    = 3        # daily pivot window each side (confirmed at d+K)
ZONE_BUF   = 0.003    # +-0.3% zone band
MIN_TOUCH  = 2        # >=2 confirmed pivots cluster -> zone
PIN_MULT   = 2.0      # lower wick >= 2x body
STOP_BUF   = 0.0075   # stop 0.75% below rejection-bar low
TP1_R, TP2_R = 1.0, 3.0
MA_TREND   = 200      # daily MA200 trend filter
SLOPE_WIN  = 20       # MA200 slope lookback (days)
MAKER_BPS  = 2.0
TAKER_BPS  = 5.0
N_SPLITS, EMBARGO, PBO_THR = 6, 50, 0.5


async def load_4h(inst: str) -> pd.DataFrame:
    conn = await asyncpg.connect(DB_DSN)
    rows = await conn.fetch(
        """SELECT bar_close_ts, open::float, high::float, low::float, close::float
           FROM market_data.ohlcv_1h WHERE instrument=$1 AND source=$2
           ORDER BY bar_close_ts""", inst, SOURCE_4H)
    await conn.close()
    df = pd.DataFrame([dict(r) for r in rows])
    df["bar_close_ts"] = pd.to_datetime(df["bar_close_ts"], utc=True)
    return df.set_index("bar_close_ts").sort_index()


def daily_from_4h(df4: pd.DataFrame) -> pd.DataFrame:
    d = df4.resample("1D").agg(open=("open","first"), high=("high","max"),
                               low=("low","min"), close=("close","last")).dropna()
    return d


def cluster_zones(prices: list[float]) -> list[tuple[float,float]]:
    """Cluster prices within +-ZONE_BUF; return [(lo,hi)] for clusters >=MIN_TOUCH."""
    if len(prices) < MIN_TOUCH:
        return []
    sp = sorted(prices)
    zones = []
    grp = [sp[0]]
    for p in sp[1:]:
        if p <= grp[0] * (1 + 2*ZONE_BUF):     # within band of group anchor
            grp.append(p)
        else:
            if len(grp) >= MIN_TOUCH:
                m = float(np.mean(grp)); zones.append((m*(1-ZONE_BUF), m*(1+ZONE_BUF)))
            grp = [p]
    if len(grp) >= MIN_TOUCH:
        m = float(np.mean(grp)); zones.append((m*(1-ZONE_BUF), m*(1+ZONE_BUF)))
    return zones


def build_daily_context(daily: pd.DataFrame):
    """Per-day: active support/resistance zones (causal) + trend state."""
    d = daily.reset_index(drop=True)
    n = len(d)
    low = d["low"].to_numpy(); high = d["high"].to_numpy(); close = d["close"].to_numpy()
    # pivots confirmed at d+K
    piv_lo_conf, piv_hi_conf = {}, {}   # confirm_index -> price
    for i in range(PIVOT_K, n-PIVOT_K):
        if low[i] == low[i-PIVOT_K:i+PIVOT_K+1].min():
            piv_lo_conf.setdefault(i+PIVOT_K, []).append(low[i])
        if high[i] == high[i-PIVOT_K:i+PIVOT_K+1].max():
            piv_hi_conf.setdefault(i+PIVOT_K, []).append(high[i])
    ma = pd.Series(close).rolling(MA_TREND).mean().to_numpy()
    sup_by_day, res_by_day, trend_by_day = {}, {}, {}
    lows_seen, highs_seen = [], []
    for i in range(n):
        for p in piv_lo_conf.get(i, []): lows_seen.append(p)
        for p in piv_hi_conf.get(i, []): highs_seen.append(p)
        sup_by_day[i] = cluster_zones(lows_seen)
        res_by_day[i] = cluster_zones(highs_seen)
        # trend: -1 strong down, +1 strong up, 0 neutral (causal)
        t = 0
        if i >= SLOPE_WIN and not np.isnan(ma[i]) and not np.isnan(ma[i-SLOPE_WIN]):
            if close[i] < ma[i] and ma[i] < ma[i-SLOPE_WIN]: t = -1
            elif close[i] > ma[i] and ma[i] > ma[i-SLOPE_WIN]: t = +1
        trend_by_day[i] = t
    dates = list(daily.index.date)
    date_to_idx = {dt: i for i, dt in enumerate(dates)}
    return sup_by_day, res_by_day, trend_by_day, date_to_idx


def is_pin_bull(o,h,l,c):
    body = abs(c-o); lw = min(o,c)-l
    return body > 0 and lw >= PIN_MULT*body and c >= (h+l)/2
def is_pin_bear(o,h,l,c):
    body = abs(c-o); uw = h-max(o,c)
    return body > 0 and uw >= PIN_MULT*body and c <= (h+l)/2
def is_engulf_bull(po,pc,o,c):
    return pc < po and c > o and c >= po and o <= pc
def is_engulf_bear(po,pc,o,c):
    return pc > po and c < o and c <= po and o >= pc


def simulate(df4: pd.DataFrame, ctx) -> tuple[np.ndarray, dict]:
    sup_by_day, res_by_day, trend_by_day, date_to_idx = ctx
    o=df4["open"].to_numpy(); h=df4["high"].to_numpy(); l=df4["low"].to_numpy(); c=df4["close"].to_numpy()
    dates=[ts.date() for ts in df4.index]
    n=len(c); ret=np.zeros(n)
    pos=0.0; side=0; entry=stop=tp1=tp2=0.0; a_open=b_open=False
    st=dict(longs=0, shorts=0, trades=0, wins=0, maker=0, taker=0)

    def cost(bps,size): return bps/1e4*abs(size)

    for i in range(1, n):
        ret[i] = pos*(c[i]-c[i-1])/(c[i-1]+1e-10)
        ci=0.0
        if pos != 0.0:
            # exits (intrabar: check stop first conservatively, then targets)
            if side==1:
                if l[i] <= stop:
                    ci+=cost(TAKER_BPS,pos); ret[i]+= pos*(stop-c[i])/(c[i-1]+1e-10); pos=0.0; a_open=b_open=False; st["trades"]+=1; st["taker"]+=1
                else:
                    if a_open and h[i] >= tp1:
                        sz=0.5; ci+=cost(MAKER_BPS,sz); ret[i]+= sz*(tp1-c[i])/(c[i-1]+1e-10); pos-=sz; a_open=False; st["maker"]+=1; st["wins"]+=1
                    if b_open and h[i] >= tp2:
                        sz=0.5; ci+=cost(MAKER_BPS,sz); ret[i]+= sz*(tp2-c[i])/(c[i-1]+1e-10); pos-=sz; b_open=False; st["maker"]+=1; st["wins"]+=1
                    if pos==0.0 and not a_open and not b_open: st["trades"]+=1
            else:
                if h[i] >= stop:
                    ci+=cost(TAKER_BPS,pos); ret[i]+= pos*(stop-c[i])/(c[i-1]+1e-10); pos=0.0; a_open=b_open=False; st["trades"]+=1; st["taker"]+=1
                else:
                    if a_open and l[i] <= tp1:
                        sz=0.5; ci+=cost(MAKER_BPS,sz); ret[i]+= (-sz)*(tp1-c[i])/(c[i-1]+1e-10); pos+=sz; a_open=False; st["maker"]+=1; st["wins"]+=1
                    if b_open and l[i] <= tp2:
                        sz=0.5; ci+=cost(MAKER_BPS,sz); ret[i]+= (-sz)*(tp2-c[i])/(c[i-1]+1e-10); pos+=sz; b_open=False; st["maker"]+=1; st["wins"]+=1
                    if pos==0.0 and not a_open and not b_open: st["trades"]+=1
            ret[i]-=ci
            if pos != 0.0:
                continue
            ret[i]-=0.0  # already applied

        # entry scan (flat) — uses zones/trend confirmed by PRIOR day (causal)
        if pos == 0.0:
            di = date_to_idx.get(dates[i])
            if di is None or di-1 < 0:
                continue
            dprev = di-1
            sups = sup_by_day.get(dprev, []); ress = res_by_day.get(dprev, []); trend = trend_by_day.get(dprev,0)
            po,pc = o[i-1],c[i-1]
            # LONG at support
            if trend != -1:
                for (zlo,zhi) in sups:
                    if zlo <= l[i] <= zhi:
                        if is_pin_bull(o[i],h[i],l[i],c[i]) or is_engulf_bull(po,pc,o[i],c[i]):
                            entry=c[i]; stop=l[i]*(1-STOP_BUF); R=entry-stop
                            if R>0:
                                tp1=entry+TP1_R*R; tp2=entry+TP2_R*R
                                pos=1.0; side=1; a_open=b_open=True; ci2=cost(MAKER_BPS,1.0); ret[i]-=ci2; st["longs"]+=1; st["maker"]+=1
                            break
            # SHORT at resistance
            if pos==0.0 and trend != +1:
                for (zlo,zhi) in ress:
                    if zlo <= h[i] <= zhi:
                        if is_pin_bear(o[i],h[i],l[i],c[i]) or is_engulf_bear(po,pc,o[i],c[i]):
                            entry=c[i]; stop=h[i]*(1+STOP_BUF); R=stop-entry
                            if R>0:
                                tp1=entry-TP1_R*R; tp2=entry-TP2_R*R
                                pos=-1.0; side=-1; a_open=b_open=True; ci2=cost(MAKER_BPS,1.0); ret[i]-=ci2; st["shorts"]+=1; st["maker"]+=1
                            break
    return ret, st


async def main(register: bool) -> None:
    print("="*76); print("Price-action S/R reversal gate — ETH, 4h, maker, 1x"); print("="*76)
    trial_n=_load_trials()["total_trials"]+1; dsr_thr=_dsr_threshold(trial_n)
    print(f"Global trial #{trial_n}  DSR bar: {dsr_thr:.3f}  maker/taker {MAKER_BPS}/{TAKER_BPS}bps")
    print(f"A-priori: pivotK={PIVOT_K} zoneBuf={ZONE_BUF} minTouch={MIN_TOUCH} stopBuf={STOP_BUF} "
          f"TP={TP1_R}R/{TP2_R}R MA{MA_TREND} trendfilter\n")
    df4=await load_4h(INSTRUMENT)
    daily=daily_from_4h(df4)
    print(f"ETH: {len(df4):,} 4h bars, {len(daily):,} daily ({df4.index[0].date()}..{df4.index[-1].date()})")
    ctx=build_daily_context(daily)
    nsup=sum(1 for v in ctx[0].values() if v);
    ret,st=simulate(df4, ctx)
    gate=_walk_forward_gate(ret, N_SPLITS, EMBARGO, PERIODS_4H, PBO_THR)
    gross=_sharpe(ret, PERIODS_4H); adj=gate["deflated_sharpe"]-dsr_thr
    status = "PASS" if (not gate["fail_reasons"] and adj>0) else "FAIL"
    n_years=len(df4)/PERIODS_4H
    print(f"\nTrades: {st['trades']}  (longs {st['longs']} / shorts {st['shorts']})  "
          f"~{st['trades']/n_years:.1f}/yr  wins(legs)={st['wins']}  maker/taker fills={st['maker']}/{st['taker']}")
    print(f"Gross Sharpe: {gross:+.3f}")
    print(f"OOS folds: {[round(x,2) for x in gate['oos_sharpes']]}")
    print(f"meanOOS={gate['mean_oos_sharpe']:+.3f}  foldStd={np.std(gate['oos_sharpes']):.3f}  "
          f"DSR={gate['deflated_sharpe']:+.3f}  adjDSR={adj:+.3f}  PBO={gate['pbo']:.2f}")
    if gate["fail_reasons"]: print("fail:", gate["fail_reasons"])
    print("\n"+"="*76); print(f"S/R REVERSAL VERDICT (ETH, N={trial_n}): {status}"); print("="*76)
    if register:
        metrics={"instruments":{INSTRUMENT:{"status":status,"dsr":gate["deflated_sharpe"],
            "pbo":gate["pbo"],"mean_oos":gate["mean_oos_sharpe"],"gross_sharpe":gross}},
            "overall":status,"note":"price-action S/R reversal ETH 4h, daily multi-touch zones + pin/engulf, 1R/3R, maker"}
        tn=_save_trial("ops/scripts/sr_reversal_gate.py (S/R reversal ETH)", status, metrics)
        print(f"\nRegistered as global trial #{tn}")
    else:
        print("\n(dry run — not registered; pass --register)")


if __name__ == "__main__":
    ap=argparse.ArgumentParser(); ap.add_argument("--register",action="store_true")
    asyncio.run(main(ap.parse_args().register))
