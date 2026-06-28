#!/usr/bin/env python3
"""DECISIVE vol test — short straddle with REAL Deribit microstructure (skew/spread/fees).

The flat-IV model was optimistic on the hedge. This re-runs the hedged short
straddle using REAL costs measured from the live Deribit chain (2026-06):
  - ATM sold at real bid (spread ~2% → ~1% haircut)
  - protective wings priced at REAL skewed IV (puts 1.2-1.6× ATM IV measured), at ask
  - Deribit fee 0.03% underlying/leg (capped 12.5% of premium)
  - weekly hedge = ±8% strangle (real tail protection), monthly = ±15%
Path = historical DVOL (ATM IV) + REAL BTC/ETH returns (real tails). NOT real
historical fills (Deribit public API serves only ~50 expired instruments, no deep
chain history); the COST side (skew/spread/fees) is real market data — that was
the decisive uncertainty. No tuning to pass.

Measured live skew (IV multiplier vs ATM DVOL):
  BTC 7d  put-8%=1.33 call+8%=0.95 | 30d put-15%=1.31 call+15%=0.91
  ETH 7d  put-8%=1.25 call+8%=0.93 | 30d put-15%=1.17 call+15%=0.96
Run:  ./venv/bin/python ops/scripts/vol_straddle_real.py
"""
from __future__ import annotations
import asyncio, numpy as np, pandas as pd, asyncpg
from scipy.stats import norm

HELIOS="postgresql://helios:helios_dev_pass@localhost:5434/helios"
HELIVEX="postgresql://helios:helios_dev_pass@localhost:5434/helivex"
ASSETS={"BTC":"BTC-USDT-SWAP","ETH":"ETH-USDT-SWAP"}
ATM_HALFSPREAD=0.01     # sell ATM at ~1% below mid (measured ATM spread ~2%)
WING_HALFSPREAD=0.05    # buy wings ~5% above mid (measured wing spread 5-20%)
FEE_RATE=0.0003         # Deribit 0.03% underlying/contract
FEE_CAP=0.125           # capped at 12.5% of option premium
# (tenor days, wing %, put IV mult, call IV mult) per asset — REAL measured skew
CFG={
 ("BTC","weekly"):(7,0.08,1.33,0.95), ("BTC","monthly"):(30,0.15,1.31,0.91),
 ("ETH","weekly"):(7,0.08,1.25,0.93), ("ETH","monthly"):(30,0.15,1.17,0.96),
}

def bs(S,K,sig,T,call):
    if T<=0 or sig<=0: return max(0.0,(S-K) if call else (K-S))
    d1=(np.log(S/K)+0.5*sig**2*T)/(sig*np.sqrt(T)); d2=d1-sig*np.sqrt(T)
    return (S*norm.cdf(d1)-K*norm.cdf(d2)) if call else (K*norm.cdf(-d2)-S*norm.cdf(-d1))

def fee(S,prem): return min(FEE_RATE*S, FEE_CAP*prem)

async def load_dvol(a):
    c=await asyncpg.connect(HELIOS); r=await c.fetch("SELECT ts,value FROM public.raw_features WHERE feature_name='DVOL' AND asset=$1 ORDER BY ts",a); await c.close()
    s=pd.Series([x["value"] for x in r],index=pd.to_datetime([x["ts"] for x in r],utc=True).normalize()); return s[~s.index.duplicated(keep="last")]
async def load_close(i):
    c=await asyncpg.connect(HELIVEX); r=await c.fetch("SELECT bar_close_ts,close::float FROM market_data.ohlcv_1h WHERE instrument=$1 AND source='okx_swap' ORDER BY bar_close_ts",i); await c.close()
    return pd.Series([x[1] for x in r],index=pd.to_datetime([x[0] for x in r],utc=True)).resample("1D").last().dropna()

def stats(r,ppy):
    r=r[~np.isnan(r)];
    if len(r)<5: return {}
    eq=np.cumsum(r); dd=eq-np.maximum.accumulate(eq); cvar=r[r<=np.quantile(r,0.05)].mean()
    return dict(n=len(r),ann=r.mean()*ppy,sharpe=r.mean()/(r.std()+1e-12)*np.sqrt(ppy),
                maxdd=dd.min(),worst=r.min(),cvar=cvar,pos=(r>0).mean())

def run(name,dvol,close):
    idx=dvol.index.intersection(close.index); dv=dvol.reindex(idx).sort_index(); px=close.reindex(idx).sort_index()
    dates=list(px.index)
    print(f"\n{'='*92}\n{name} ({len(idx)} days {idx.min().date()}→{idx.max().date()}) — REAL skew/spread/fees\n{'='*92}")
    for tname in ("weekly","monthly"):
        T,W,pmult,cmult=CFG[(name,tname)]; Ty=T/365.0; ppy=365.0/T
        naked,hedged=[],[]; eaten=[]
        i=0
        while i+T<len(dates):
            S=px.iloc[i]; sig=dv.iloc[i]/100.0; ST=px.iloc[i+T]
            if not(S>0 and ST>0 and sig>0): i+=T; continue
            # SELL ATM straddle at bid + fees
            cprem=bs(S,S,sig,Ty,True); pprem=bs(S,S,sig,Ty,False)
            sold=(cprem+pprem)*(1-ATM_HALFSPREAD) - fee(S,cprem) - fee(S,pprem)
            move=abs(ST-S)
            naked.append((sold-move)/S)
            # BUY protective strangle at REAL skewed IV + ask + fees
            Ku,Kd=S*(1+W),S*(1-W)
            wc=bs(S,Ku,sig*cmult,Ty,True); wp=bs(S,Kd,sig*pmult,Ty,False)
            wcost=(wc+wp)*(1+WING_HALFSPREAD) + fee(S,wc) + fee(S,wp)
            wpay=max(ST-Ku,0)+max(Kd-ST,0)
            hedged.append((sold - wcost - move + wpay)/S)
            eaten.append(wcost/(cprem+pprem))
            i+=T
        sn=stats(np.array(naked),ppy); sh=stats(np.array(hedged),ppy)
        print(f"\n  ── {tname} ({T}d, {sn.get('n',0)} rolls, ±{int(W*100)}% wings @ put×{pmult} call×{cmult}; wing eats {np.mean(eaten)*100:.0f}% of premium) ──")
        for tag,s in [("NAKED",sn),(f"HEDGED ±{int(W*100)}%",sh)]:
            if s: print(f"    {tag:16s} annRet={s['ann']*100:+6.1f}%  Sharpe={s['sharpe']:+5.2f}  maxDD={s['maxdd']*100:6.1f}%  worst1={s['worst']*100:+6.1f}%  CVaR5={s['cvar']*100:+6.1f}%  pos={s['pos']*100:.0f}%")

async def main():
    print("="*92); print("DECISIVE — short straddle with REAL Deribit skew/spread/fees (cost side = real market data)")
    print("path=historical DVOL+real returns; NOT real fills (no deep chain history). per spot notional, unlevered."); print("="*92)
    for n,i in ASSETS.items(): run(n,await load_dvol(n),await load_close(i))
    print(f"\n{'='*92}\nDECISIVE Q: BTC weekly HEDGED Sharpe >0.3 → vol has something (thin); ~0/neg → vol exhausted.")
    print("Caveat even if positive: 2024-26 window had NO true black-swan → real tail risk > shown.\n"+"="*92)

if __name__=="__main__": asyncio.run(main())
