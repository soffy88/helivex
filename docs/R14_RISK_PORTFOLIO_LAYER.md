# R14 — Portfolio Risk Layer + Allocation Scaffolding

> Modules: `paper/risk.py` (pre-trade gate + circuit breakers + kill-switch),
> `tools/portfolio.py` (allocation scaffolding). Tests: `tests/test_risk_portfolio.py` (9).
> Wired into: all 4 paper strategies (pre-trade gate), `paper/alerter.py` (breakers).
> **Status: live-ready on next node/monitor restart. Does NOT depend on any alpha.**

This is the layer that protects the paper book *regardless of edge* — the gap called
out in the audit (per-strategy `qty_usd` existed; portfolio-level risk did not).

## Part 1 — Risk / circuit-breaker layer (`paper/risk.py`)

Two halves, deliberately split across the two paper processes:

**Pre-trade gate (in-process, deterministic)** — `RISK.gate_entry()`. Each strategy
calls it before submitting an ENTRY (exits are never blocked — always de-risk). Hard
caps, env-overridable:

| cap | env | default |
|---|---|---|
| portfolio gross notional | `HELIVEX_PORTFOLIO_GROSS_CAP_USD` | 3000 |
| per-strategy notional | `HELIVEX_PER_STRATEGY_CAP_USD` | 1200 |
| per-instrument concentration | `HELIVEX_PER_INSTRUMENT_CAP_USD` | 1000 |
| max concurrent positions | `HELIVEX_MAX_CONCURRENT_POSITIONS` | 12 |

The book runs in one TradingNode process, so a module-level `RISK` singleton sees
every strategy's open notional via an in-memory registry (`open_position` /
`close_position`, wired at each strategy's signal-fire point). `gate_entry` **never
raises** — on any internal error it fails OPEN (allow + log), because a risk-module
bug must never silently halt paper trading. The kill-switch is the one hard stop.

**Circuit breakers (out-of-process, periodic)** — two evaluators run by the existing
`AlerterEngine` (every 120 s, same loop as the health checks):

- `eval_portfolio_drawdown` — realized-NAV drawdown ≥ `max_drawdown_pct` (15%) → trip.
- `eval_daily_loss` — today's realized loss ≤ `-daily_loss_limit_usd` (250) → trip.

NAV = `BASE_EQUITY_USD` + all-time **realized** P&L (avg-cost round-trip matched from
`paper.fills`). v1 limitation (documented): open positions are not marked to market,
so a large *unrealized* drawdown is caught only on close or via the daily-loss limit.

**Kill-switch (cross-process)** — a file flag (`/tmp/helivex_paper_killswitch`). The
monitor process trips it on breach; the node process's `gate_entry` reads it and
refuses all new entries. Every trip is audited to a new `paper.risk_events` table.

```
python -m paper.risk status        # caps, NAV, drawdown, kill-switch
python -m paper.risk trip "msg"    # manual halt
python -m paper.risk reset         # clear
```

Verified end-to-end against the live DB: gate caps enforced, forced-trip halts entries,
idempotent, audit row written, reset clears, healthy state silent.

## Part 2 — Allocation scaffolding (`tools/portfolio.py`)

**Why now, with nothing to allocate:** 13/13 gate trials FAIL and the book barely
trades — there is no live alpha to combine *today*. This is the tested machinery for
the moment a strategy clears the gate, because the entire reason to run several weak,
uncorrelated alphas is that the **portfolio** Sharpe can exceed any single one's.

Allocators (numpy-only): `inverse_variance_weights`, `vol_target_weights` (inverse-vol
+ leverage to a target σ), `risk_parity_weights` (ERC — sqrt-damped multiplicative
fixed point; the naive 1/marginal update oscillates and collapses to a corner), and
`fractional_kelly_weights` (long-only clipped Σ⁻¹μ).

**Thesis demo** (`--demo`, deterministic, synthetic — *not* helivex data): three weak
alphas (Sharpe 0.53 / 0.59 / 0.66, mean ρ ≈ 0) → portfolio Sharpe ≈ **1.05** under
every return-agnostic allocator — a ~58% lift over the best single, purely from
diversification. The default CLI reads real per-strategy daily returns from
`paper.fills` and falls back to the labelled demo when history is too thin.

**Link to Part 1:** the allocator decides *how much* of the risk budget each strategy
gets (target weight × budget → target notional); the pre-trade gate enforces it as the
hard `PER_STRATEGY_CAP`. Allocation proposes, the risk gate disposes.

## Activation

Strategy edits and breaker wiring take effect on the next **node** and **monitor**
restart respectively. The layer is fail-safe: absent kill-switch = allow, generous
default caps, breakers silent until a real breach.
