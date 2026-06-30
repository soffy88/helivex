#!/usr/bin/env python3
"""Compound-B probe — nonlinear/conditional signal interactions via GBM, strictly framed.

Hypothesis: single signals show OOS corr ~0, but "signal A may only work under
condition B" — a nonlinear interaction R11's linear soft-router (FAIL) couldn't
capture. Test with gradient boosting (interpretable, less overfit-prone than nets)
under HARD anti-p-hacking discipline.

Anti-p-hacking discipline:
  - features a-priori, ≤10 (no fishing): 4 price-derived + 5 helios-real + 1 macro
  - hyperparams FIXED a-priori (depth 3, lr 0.03, l2 1.0, min_leaf 50, seed 42),
    NO tuning to score; early-stopping on an internal val split is the only fit knob
  - strict expanding walk-forward, causal features (merge_asof, no look-ahead),
    target = forward return (label only)
  - decisive metric = OOS corr persistence (train IC vs test IC, split halves) —
    is it real interaction or just in-sample fit on noise?
  - permutation importance — what does it lean on (single noise feature?)
  - full helivex gate (DSR/PBO/global-N) on OOS pnl → trial #12

Data: BTC 4h, real-feature overlap ~2025-06→2026-06 (~2275 bars, funding+OI+DVOL
all present). Short window is a limitation (flagged) but OOS corr~0 would be robust
regardless of n.
Run:  ./venv/bin/python ops/scripts/gbm_compound_gate.py [--register]
"""
from __future__ import annotations
import argparse, asyncio, sys, warnings
from pathlib import Path
import asyncpg, numpy as np, pandas as pd
from scipy.stats import spearmanr

PROJ = Path(__file__).parent.parent.parent
sys.path.insert(0, str(PROJ))
from tools.strategy_gate import _walk_forward_gate, _sharpe, _dsr_threshold, _load_trials, _save_trial  # noqa
warnings.filterwarnings("ignore")

HELIOS="postgresql://helios:helios_dev_pass@localhost:5434/helios"
HELIVEX="postgresql://helios:helios_dev_pass@localhost:5434/helivex"
START="2025-06-01"
H = 6                 # forward target horizon (6×4h = 24h)
PERIODS_4H = 365*6
RETRAIN = 180         # walk-forward retrain step (bars)
COST_BPS = 5.0
SEED = 42
N_SPLITS, EMBARGO, PBO_THR = 6, 50, 0.5
FEATURES = ["trend","mr_z","rv","funding","dfunding","log_oi","doi","dvol","vrp","vix"]

GB_PARAMS = dict(max_depth=3, learning_rate=0.03, max_iter=400, min_samples_leaf=50,
                 l2_regularization=1.0, early_stopping=True, validation_fraction=0.15,
                 n_iter_no_change=20, random_state=SEED)


async def _q(dsn, sql, *a):
    c=await asyncpg.connect(dsn); r=await c.fetch(sql,*a); await c.close(); return r

async def build():
    bars=await _q(HELIVEX,"SELECT bar_close_ts ts, close::float c FROM market_data.ohlcv_1h WHERE instrument='BTC-USDT-SWAP' AND source='okx_swap' AND bar_close_ts>=$1 ORDER BY bar_close_ts", pd.Timestamp(START,tz='UTC'))
    df=pd.DataFrame([dict(r) for r in bars]); df["ts"]=pd.to_datetime(df["ts"],utc=True); df=df.set_index("ts")
    # helios features
    async def hf(asset,fn):
        r=await _q(HELIOS,"SELECT ts,value FROM public.raw_features WHERE asset=$1 AND feature_name=$2 ORDER BY ts",asset,fn)
        s=pd.DataFrame([dict(x) for x in r]); s["ts"]=pd.to_datetime(s["ts"],utc=True); return s.sort_values("ts")
    fund=await hf("BTC","funding_rate"); oi=await hf("BTC","open_interest_usd"); dvol=await hf("BTC","DVOL"); vix=await hf("US-MACRO","VIXCLS")
    base=df.reset_index()[["ts","c"]]
    def asof(s,col):
        return pd.merge_asof(base, s.rename(columns={"value":col}), on="ts", direction="backward")[col].values
    df["funding"]=asof(fund,"funding"); df["open_interest"]=asof(oi,"oi"); df["dvol"]=asof(dvol,"dvol"); df["vix"]=asof(vix,"vix")
    c=df["c"]
    df["ret"]=c.pct_change()
    df["trend"]=c/c.rolling(50).mean()-1
    sma20=c.rolling(20).mean(); std20=c.rolling(20).std()
    df["mr_z"]=(c-sma20)/(std20+1e-9)
    df["rv"]=df["ret"].rolling(20).std()*np.sqrt(PERIODS_4H)*100
    df["dfunding"]=pd.Series(df["funding"]).diff()
    df["log_oi"]=np.log(df["open_interest"].clip(lower=1))
    df["doi"]=pd.Series(df["open_interest"]).pct_change()
    df["vrp"]=df["dvol"]-df["rv"]
    df["target"]=c.shift(-H)/c-1
    df=df.dropna(subset=FEATURES+["target","ret"])
    return df

def walk_forward(X,y,fwd):
    from sklearn.ensemble import HistGradientBoostingRegressor
    n=len(X); preds=np.full(n,np.nan)
    init=int(n*0.5)
    t=init
    while t<n:
        m=HistGradientBoostingRegressor(**GB_PARAMS)
        m.fit(X[:t],y[:t])
        te=min(t+RETRAIN,n)
        preds[t:te]=m.predict(X[t:te])
        t+=RETRAIN
    return preds, init

def ic_halves(p,a):
    m=~(np.isnan(p)|np.isnan(a)); p,a=p[m],a[m]
    if len(p)<30: return float("nan"),float("nan"),float("nan"),len(p)
    h=len(p)//2
    return (spearmanr(p,a).correlation, spearmanr(p[:h],a[:h]).correlation,
            spearmanr(p[h:],a[h:]).correlation, len(p))

async def main(register):
    print("="*82); print("Compound-B GBM probe — nonlinear signal interactions (BTC 4h, real features)")
    print("strict walk-forward · ≤10 a-priori features · fixed hyperparams · OOS-corr decisive"); print("="*82)
    df=await build()
    print(f"\nBTC 4h: {len(df)} bars {df.index[0].date()}→{df.index[-1].date()}  features={FEATURES}")
    X=df[FEATURES].to_numpy(float); y=df["target"].to_numpy(float); ret=df["ret"].to_numpy(float)
    trial_n=_load_trials()["total_trials"]+1; dsr_thr=_dsr_threshold(trial_n)

    preds,init=walk_forward(X,y,y)
    # in-sample IC (train region, refit once on first 50%) vs OOS
    from sklearn.ensemble import HistGradientBoostingRegressor
    m0=HistGradientBoostingRegressor(**GB_PARAMS); m0.fit(X[:init],y[:init])
    is_ic=spearmanr(m0.predict(X[:init]), y[:init]).correlation
    oos_full,oos_h1,oos_h2,noos=ic_halves(preds[init:], y[init:])
    print(f"\n── Predictive power ──")
    print(f"  IN-SAMPLE IC (train 50%):     {is_ic:+.3f}")
    print(f"  OOS IC (walk-forward test):   {oos_full:+.3f}   halves {oos_h1:+.3f}/{oos_h2:+.3f}   nOOS={noos}")

    # permutation importance on OOS block (representative final model)
    from sklearn.inspection import permutation_importance
    split=int(len(df)*0.7)
    mP=HistGradientBoostingRegressor(**GB_PARAMS); mP.fit(X[:split],y[:split])
    pi=permutation_importance(mP, X[split:], y[split:], n_repeats=10, random_state=SEED, scoring="r2")
    print("  permutation importance (OOS, r2 drop):")
    for i in np.argsort(pi.importances_mean)[::-1]:
        print(f"     {FEATURES[i]:10s} {pi.importances_mean[i]:+.4f}")

    # gate on OOS pnl: position = sign(pred), per-bar pnl
    pos=np.zeros(len(df)); pos[init:]=np.sign(np.nan_to_num(preds[init:]))
    pnl=np.zeros(len(df))
    pnl[init+1:]=pos[init:-1]*ret[init+1:]
    turn=np.abs(np.diff(pos[init:],prepend=0.0))
    pnl[init:]-=turn*(COST_BPS/1e4)
    oos_pnl=pnl[init:]
    gate=_walk_forward_gate(oos_pnl,N_SPLITS,EMBARGO,PERIODS_4H,PBO_THR)
    adj=gate["deflated_sharpe"]-dsr_thr
    status="PASS" if (not gate["fail_reasons"] and adj>0) else "FAIL"
    fstd=float(np.std(gate["oos_sharpes"])) if gate["oos_sharpes"] else float("nan")
    print(f"\n── Gate on OOS pnl (sign-of-pred, {COST_BPS}bps) — trial #{trial_n}, bar {dsr_thr:.3f} ──")
    print(f"  gross={_sharpe(oos_pnl,PERIODS_4H):+.3f}  meanOOS={gate['mean_oos_sharpe']:+.3f}  foldStd={fstd:.3f}  "
          f"DSR={gate['deflated_sharpe']:+.3f}  adjDSR={adj:+.3f}  PBO={gate['pbo']:.2f}")
    print(f"\n{'='*82}\nCOMPOUND-B VERDICT (N={trial_n}): {status}\n{'='*82}")
    print("Read: OOS IC ≫0 AND stable across halves → real interaction. OOS IC~0 / flips →")
    print("in-sample fit on noise (regime-style false positive). is_ic≫oos_ic = overfitting.")

    if register:
        metrics={"instruments":{"BTC-USDT-SWAP":{"status":status,"dsr":gate["deflated_sharpe"],
            "pbo":gate["pbo"],"mean_oos":gate["mean_oos_sharpe"],"gross_sharpe":_sharpe(oos_pnl,PERIODS_4H)}},
            "overall":status,"note":f"GBM compound nonlinear, BTC 4h, {len(FEATURES)} feats, OOS IC {oos_full:+.3f}"}
        tn=_save_trial("ops/scripts/gbm_compound_gate.py (GBM compound BTC)",status,metrics)
        print(f"\nRegistered as global trial #{tn}")
    else:
        print("\n(dry run — not registered; pass --register)")

if __name__=="__main__":
    ap=argparse.ArgumentParser(); ap.add_argument("--register",action="store_true")
    asyncio.run(main(ap.parse_args().register))
