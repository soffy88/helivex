# R15 — Data-Lever Audit (helios / helixa): L2, Cross-Exchange, On-Chain

> Scope: can helios/helixa supply a NEW, backtestable alpha input after directional /
> regime / volatility were all exhausted on OHLCV+funding+DVOL+macro?
> Probe: `ops/scripts/helios_fusion_ic_probe.py` (NOT a gate, N stays 13).
> Method: real DB + Redis inventory of helios (pg `helios`, redis :6381) and helixa,
> plus an IC probe of helios's 13 fused dimensions vs its own forward-return labels.
> **Verdict: no NEW backtestable lever today. Do NOT build capture infra yet. The one
> cheap path is the self-accumulating fused-dim dataset — re-probe as history grows.**

## The premise, corrected

"helios has L2 / cross-exchange / on-chain" is true at the *live-serving* level but
**not at the backtestable-history level**, which is what a gate needs.

| Lever | Reality | Backtestable? |
|---|---|---|
| **L2 order book** | helixa collects Binance top-10 every 3s → **Redis 10s TTL only**; its `orderbook` table + `insert_orderbook()` exist but are **never called** (explicit "数据量太大" comment). helios reads it live, doesn't store. helixa data-collector not currently running. | **NO** — zero history, not even being captured |
| **Cross-exchange** | funding (dydx/hyperliquid/coinalyze), OI (binance/dydx) in `raw_features`. No unified spot tape / no cross-venue spread. | only the derivatives aggregates — **already gated to FAIL** (regime probe, commit 646dc71) |
| **On-chain** | CEX netflow / stablecoin supply / liquidation heatmap / whale clearinghouse — live in **helios redis (ephemeral, TTL 1–4k s)**, NOT persisted to pg. | **NO** durable history |

## Decisive finding 1 — the only data with real history is the data that already failed

`raw_features` (the one durable feature store) per-feature history:

- ≥1yr history: `dydx funding` (13mo), `binance_vision OI` (1yr, BTC), `deribit DVOL`
  (2yr), `FRED macro` (3yr) — **all already used by the regime/vol gates → OOS corr ≈0
  / closed** (trials, docs R11–R13).
- Everything genuinely new (`hyperliquid_ctx`, `dydx OI/volume`, fused dims, forward
  returns): **3–5 days of history only**. `ts_fusion_dimensions` since 2026-06-23,
  `ts_forward_returns` since 2026-06-24. Unusable for CPCV/DSR/PBO.

## Decisive finding 2 — fused-dim IC probe: inconclusive, weakly positive, NOT gateable

helios persists, going forward, 13 fused dimensions per (time, symbol) **and** a
ready-made forward-return label. The probe asks: does any dim carry OOS-stable rank
correlation with the 60-min forward return? (BTC/ETH/SOL, ~3–5 days.)

- 6 of 13 dims are **constant** over the window (volume, macro, regime, seasonality,
  engine_consensus, decision_trail_history) → no information here yet.
- In the honest 60-min **non-overlapping** subsample (**n≈71/symbol** — tiny), only
  `sector_rotation` is consistently positive across both halves and both views
  (IC ≈ **+0.08**). `trend` and `support_resistance_distance` flag marginally.
- **But IC 0.08 at n=71 is noise-level** (Spearman SE ≈ 1/√71 ≈ 0.12). Over 3 days it
  could be one trending episode. The overlapping view (n≈11k) is autocorr-inflated and
  not trustworthy. **No p-value is meaningful here.**
- Importantly the fused `onchain` and `flow` dims — derived from the raw flow streams —
  show **no** stable signal (flow negative/unstable, onchain ≈0). So helios's own
  aggregation of the on-chain data does not predict majors' returns in this window.

Read: the fused dims are **not obviously dead** like regime was (a few show small
consistent positive IC), but nowhere near gateable, and the on-chain/flow content
specifically shows nothing.

## Decisive finding 3 — the raw flow streams are mostly empty right now

Sampled live (helios redis :6381 DB0): `exchange_netflow:*` → `net_24h: 0.0` (in/out
all zero), `liquidation_heatmap:*` → empty buckets / `total_count_24h: 0`,
`stablecoin_supply_change` → `net_mint_burn: null`. Only `tvl_momentum` and stablecoin
`change_7d_pct` are populated. The Ethereum/Tron/Solana flow collectors are returning
blanks — capturing them now would mostly persist zeros.

## Decision

**Do NOT build a capture pipeline yet.** Building infra to persist (a) L2 that isn't
even being collected, or (b) flow streams that are mostly empty and whose fused form
already shows no signal, is the same speculative-infra mistake the VRP-options closure
(R13) avoided. Evidence does not justify it.

**Instead — the free path:** `ts_fusion_dimensions` + `ts_forward_returns` already
accumulate durably in helios postgres at no cost to us. In 1–3 months there will be
enough non-overlapping history to gate properly. The action is **patience + re-probe**,
not a build.

### Concrete next steps (cheap, evidence-gated)
1. **Re-run the probe monthly** as history grows:
   `./venv/bin/python ops/scripts/helios_fusion_ic_probe.py`
   Trigger to escalate to a real gate: ≥6–8 weeks of data AND a dim holding
   |IC| ≳ 0.05 with stable sign on the non-overlapping subsample (n in the hundreds).
2. **L2 microstructure (deferred, cheap to start IF pursued):** helixa already has the
   `orderbook` table + an uncalled `insert_orderbook()`. Enabling durable top-of-book
   capture there is a small wiring change — worth doing **only** if microstructure is
   chosen as a deliberate direction (it's the one truly-untested data class, but high
   volume and zero current history). Not justified by any evidence today.
3. **Raw flow capture (deferred):** revisit only if the flow collectors start returning
   non-zero AND a future fused-dim probe shows onchain/flow life.

## Bottom line

The data bottleneck is real but **helios/helixa do not relieve it today**: their
deep-history data is the same derivatives+macro that already failed, and their
genuinely-new data is either live-only/ephemeral, mostly empty, or only days old. No
new gate is launchable now. The single positive: a clean supervised dataset (fused dims
→ forward returns) is building itself for free — the disciplined move is to let it
accumulate and re-probe, spending nothing until the evidence earns it.
