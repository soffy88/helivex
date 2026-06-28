# R13 — Short-Vol / VRP Harvest (Deribit straddle, real-cost) — CLOSED

> Scripts: `ops/scripts/vol_trading_probe.py` (feasibility), `vol_straddle_backtest.py`
> (tail-honest model), `vol_straddle_real.py` (**decisive, real Deribit costs**)
> Data: helios **DVOL** (Deribit 30d implied, BTC/ETH 2yr daily) + helivex OHLCV
> realized; cost side = **live Deribit smile measured 2026-06** (real skew/spread/fees)
> Window: 723 days **2024-06-23 → 2026-06-15**
> **Verdict: NO-GO. Ruin-safe (hedged) Sharpe ≤ 0. Global trial #13, FAIL.**
> **This closes the volatility / VRP direction alongside directional & regime.**

## Hypothesis (built on the probe, not blind)

The vol feasibility probe (`vol_trading_probe.py`) found the **one non-dead signal**
after 11/11 directional+regime FAIL: a real, persistent **volatility risk premium**.
BTC implied (DVOL avg 48.6) > realized (RV20 44.2) **73–76% of the time** — the
classic crypto VRP, unlike regime's OOS corr ≈ 0. The question R13 settles: **can a
risk-managed retail seller actually harvest it after real option costs?**

Critically, the edge is the **unconditional premium** (short vol always), *not* a
timed signal — IC(VRP, payoff) ≈ 0 and flips sign across halves. So "harvest" = sell
ATM straddles continuously and survive the fat left tail. That requires a tail hedge;
the whole question is whether the premium survives the hedge's real cost.

## Why this is decisive (the cost side is real market data)

No deep historical Deribit chain exists in any DB (the public API serves only ~50
expired instruments), so this is **not a real-fill backtest** — it is a model:
Black-Scholes premiums from historical DVOL, payoffs from **real BTC/ETH price paths
(real tail days included)**. But the *decisive uncertainty was never the premium —
it was the hedge cost*, and **that side is real**: the protective wings are priced at
the **live measured Deribit skew**, sold at real bid, with real fees.

Measured live skew (IV multiplier vs ATM DVOL, 2026-06):

| asset | tenor | put wing | call wing |
|---|---|---|---|
| BTC | 7d  | −8% put = **1.33×** | +8% call = 0.95× |
| BTC | 30d | −15% put = **1.31×** | +15% call = 0.91× |
| ETH | 7d  | −8% put = **1.25×** | +8% call = 0.93× |
| ETH | 30d | −15% put = **1.17×** | +15% call = 0.96× |

Costs applied: ATM sold ~1% below mid (measured spread ~2%), wings bought ~5% above
mid, Deribit fee 0.03% underlying/leg capped at 12.5% of premium. **No tuning to pass.**

## §1 Result — short ATM straddle, naked vs ruin-safe hedged (per spot notional, unlevered)

| asset / tenor | NAKED Sharpe | NAKED worst1 | HEDGED Sharpe | wing eats |
|---|---|---|---|---|
| BTC weekly (±8% wings)   | **+1.30** | −10.1% | **−0.41** | 25% |
| BTC monthly (±15% wings) | +0.30 | −26.2% | **+0.01** | 25% |
| ETH weekly (±8% wings)   | +0.17 | −30.1% | **−1.64** | 36% |
| ETH monthly (±15% wings) | −0.19 | −40.7% | **−0.25** | 38% |

Decision rule (pre-registered in the script): *BTC weekly HEDGED Sharpe > 0.3 → vol
has something; ≈ 0 or negative → exhausted.* Result: **−0.41 → exhausted.**

## §2 Cause of death — VRP ≈ the fair price of tail risk

The premium is real, but **real put skew (1.2–1.6× ATM) makes the protective wings
dear enough to eat 25–38% of the premium** — and once the book is made ruin-safe, the
remaining edge is **negative (BTC weekly −0.41, ETH weekly −1.64) or flat (BTC monthly
+0.01)**. The only positive number is the **naked** book (BTC weekly +1.30), which:

1. carries **uncapped left-tail / ruin risk** — a single −40% day blows up a naked or
   levered short-vol account, and
2. is **survivorship-flattered**: the 2024-06 → 2026-06 window had **no true
   black-swan** (worst BTC week only −10%), so realized tail risk understates the truth.

This is the efficient-market outcome: a systematic **retail** vol-seller paying Deribit
retail costs cannot harvest the VRP **risk-managed**. (A market-maker *earning* the
spread is a different business — not reachable from this stack.)

## Caveats (honest)

- BTC/ETH only; DVOL is the 30d index, not the full surface.
- 2-year window with **no genuine black-swan** → naked numbers are optimistic.
- Model premiums (BS r=0), not real historical fills — but the **cost side that
  decides it is real measured market data**, which is the point.
- ETH has **no premium to begin with** (DVOL ≈ RV) and fatter tails → negative throughout.

## Verdict

**NO-GO. Volatility / VRP direction CLOSED.** Gate ledger trial **#13 FAIL**. Not worth
months of options-execution infrastructure to harvest a premium that real Deribit skew
prices to ≈ fair. The minor ETH IV-timing signal (IC ≈ 0.15, both halves positive)
survives only as a *possible weak directional overlay*, not a standalone strategy.

Directions exhausted to date: **directional / trend / MR / scalp / S/R / pairs /
funding-arb / HMM-regime (per-asset & cross-asset) / GBM-compound / volatility-VRP.**
The live bottleneck remains **data breadth** (no L2 book, no cross-venue, no on-chain,
no real options chain), not research discipline.
