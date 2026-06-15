# R6: Paper Trading Framework — Honest Holdout Verification

**Date**: 2026-06-15
**Status**: Infrastructure complete, pending OKX Demo key verification

---

## §0 Framework Design Principle

Paper trading is **NOT** a short-term profit signal.

Paper = honest holdout verification of backtest execution assumptions:
- Does signal_price (bar close) match actual fill price? (slippage)
- Does limit order fill rate match backtest assumption?
- Does market order IOC actually fill at close? (latency)

**Gate**: paper fidelity data → update backtest cost model → re-run gate → then decide on live.
Paper profit ≠ valid. Only execution assumption match matters.

---

## §1 Strategy 3 Backtest (R6.0)

**Method**: Daily Donchian Spot Trend, long-only, 200d MA bear filter.
Resampled from 5m SWAP data to 1D proxy (spot ≈ perp during non-funding hours).

**Results**:
| Instrument | Gross Sharpe | @10bps | Gate DSR | PBO | Verdict |
|------------|-------------|--------|----------|-----|---------|
| BTC 1D     | ~1.35        | ~1.11  | -0.185   | 1.0 | FAIL    |
| ETH 1D     | ~1.28        | ~1.05  | -0.217   | 1.0 | FAIL    |
| SOL 1D     | ~1.50        | ~1.28  | -0.142   | 1.0 | FAIL    |

**Root cause of gate fail**: Same structural issue as R4.4.
Long-only IS periods straddle bull phases (BTC 2020–2021, 2023–2024) while OOS periods may hit bear legs.
IS Sharpe consistently > OOS Sharpe across all folds → PBO = 1.0 by construction.

**Trade frequency**: ~5 round-trips/year → cost-insensitive (gross vs @10bps gap is tiny).
Alpha mechanism is real but CPCV cannot evaluate it without long/short symmetry.

**Verdict: NO-GO (backtest gate)**. Paper running for execution fidelity validation only,
NOT as alpha signal confirmation.

---

## §2 Three Strategies on OKX Demo

### Strategy 1: 4H Donchian Trend (SWAP)
- **Instruments**: BTC-USDT-SWAP, ETH-USDT-SWAP, SOL-USDT-SWAP
- **Parameters**: N_ENTER=20, N_EXIT=10, qty_usd=200
- **Execution**: Taker market (IOC)
- **Backtest reference**: R4.4 (gate FAIL, PBO=1.0, same long-only IS/OOS imbalance)
- **Paper purpose**: Validate taker slippage at 4H bar close

### Strategy 2: VWAP-MR 1H (SOL SWAP)
- **Instrument**: SOL-USDT-SWAP
- **Parameters**: vwap_n=4 (4H window), z_thr=2.0, hold=6H
- **Execution**: Taker market (IOC)
- **Backtest reference**: R5.3 — SOL 1H gross=+1.155, @10bps barely +0.074, gate FAIL (DSR=-1.436, PBO=0.86)
- **Paper purpose**: Validate fill rate (backtest assumed 97% fill on 1H close)

### Strategy 3: Daily Donchian Spot (BTC, ETH)
- **Instruments**: BTC-USDT, ETH-USDT (spot)
- **Parameters**: N_ENTER=20, N_EXIT=10, 200d MA bear filter
- **Execution**: Taker market (IOC)
- **Backtest reference**: R6.0 (gate FAIL, PBO=1.0)
- **Paper purpose**: Validate daily bar close → market fill slippage

---

## §3 Infrastructure

### Paper package structure
```
paper/
  __init__.py          — package docstring
  audit.py             — GOLD Ed25519 signing (omodul.audit_record wrapper)
  db.py                — TimescaleDB schema + signal/fill logging
  node.py              — NautilusTrader TradingNode (6 strategy instances)
  run.py               — Launcher (loads .env, safety gate, init DB, run node)
  strategies/
    __init__.py
    donchian_4h.py     — Strategy 1 NT adapter
    vwap_mr_1h.py      — Strategy 2 NT adapter
    spot_trend_1d.py   — Strategy 3 NT adapter
```

### DB Schema
```sql
paper.signals          — every signal decision (pre-order), Ed25519 signed
paper.fills            — every fill (post-execution), slippage_bps computed
paper.fidelity_summary — computed aggregates
```

### Audit chain
Every signal before order submission:
1. `sign_signal(event_body)` → SHA-256 fingerprint + Ed25519 signature
2. `log_signal(...)` → `paper.signals` row with `audit_record_id`, `fingerprint_hex`, `sig_b64`
3. On fill: `log_fill(...)` → `paper.fills` row with `slippage_bps = direction * (actual - signal) / signal * 10_000`

GOLD tier: keys in `HELIVEX_AUDIT_PRIVATE_KEY_B64` / `HELIVEX_AUDIT_PUBLIC_KEY_B64`.
STANDARD tier (fallback): fingerprint only, no signature.

---

## §4 Execution Fidelity Measurement

**What we measure (paper = honest)**:

| Metric | Source | Backtest Assumption | Pass Threshold |
|--------|--------|---------------------|----------------|
| Taker slippage | `paper.fills.slippage_bps` | 5bps/side at bar close | <8bps p95 |
| Fill rate (IOC) | n_fills / n_signals | 100% (market order) | >95% |
| Latency | ts_fill - ts_signal | Not modeled | Record only |

**Decision tree after 30 days of paper**:
- If mean_slippage < 5bps → backtest cost model conservative → gate re-run with actual cost
- If mean_slippage > 8bps → backtest cost model optimistic → strategies even less viable
- If fill_rate < 95% → IOC model needs revision (partial fills, insufficient liquidity)

---

## §5 Launch Prerequisites

- [ ] Verify `/home/soffy/projects/selene/.env` OKX keys are **Demo** keys (not live)
- [ ] Create `helivex/.env` with OKX Demo credentials + Ed25519 keys
- [ ] Run `python paper/db.py` to init `paper.*` tables
- [ ] Run `python paper/run.py` (safety gate blocks if `OKX_LIVE=1`)

---

## §6 Honest Judgment Framework

**What paper proves / does NOT prove**:

| Claim | Evidence needed | NOT proven by |
|-------|----------------|---------------|
| Backtest slippage assumption is realistic | paper fills match signal_price ± threshold | short-term paper P&L |
| Strategy alpha is real (not backtest overfit) | CPCV gate pass | paper profit (luck) |
| Safe to deploy live | Gate pass + paper fidelity match | paper paper profit |

**Current R6 status**: All 3 strategies are **backtest gate FAIL**.
Running paper solely for execution fidelity data to:
1. Update cost model for any future gate re-run
2. Establish a live holdout period to address the long-only CPCV structural limitation
3. Understand OKX Demo fill characteristics before any future live consideration

**When can live deployment be considered**:
- Need either: (a) new strategy that passes CPCV gate, OR (b) 2+ year live holdout showing robust OOS
- Paper profit alone is NOT sufficient, per project constraint: "paper赚≠有效(短期运气)"
