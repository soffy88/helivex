# R11 — HMM Regime-Adaptive Combination of the Three Strategies

> Script: `ops/scripts/hmm_regime_combo_gate.py`
> Data: `okx_swap` 4H → daily, BTC/ETH/SOL, 2019-12 → 2026-06 (R4's data set)
> Gate: identical helivex CPCV gate as the 6 prior trials (`tools/strategy_gate._walk_forward_gate`)
> Global trial **#7**, DSR selection-bias bar = **1.387**

## Thesis under test

Every helivex strategy fails the gate solo because each only earns in *one*
regime and bleeds in the others, inflating cross-fold variance. Can an HMM
3-state regime **router** combine the three real strategy signals into one
return stream that is smoother (lower fold variance) and finally clears the gate?

Not a new strategy — the legs are the deployed live signals:
- **trend leg** = Donchian 20/10 dual-direction (`donchian_4h`)
- **MR leg** = z-vs-VWAP mean reversion (`vwap_mr_1h`)
- `spot_trend_1d` = long-only Donchian = the long side of the trend leg (subsumed)

Soft router (probability-weighted, **not** R4's on/off filter):

```
pos = P(bull)·trend_long  +  P(neutral)·mean_reversion  +  P(bear)·trend_short
```

## Design (paper best-practice; avoids R4's pitfalls)

| Choice | R11 | R4 (chop_filter) |
|---|---|---|
| States | **3** (bull/neutral/bear) | 2 (trend/chop) |
| Features | **[return, 20d realized vol]** | [dir-efficiency, \|bar ret\|] |
| Switching | **soft** (posterior-weighted) | hard on/off (flat in chop) |
| Covariance | full, `random_state=42` | full, `random_state=42` |
| Walk-forward | 2yr window, monthly retrain | 5-fold IS/OOS |
| Regime for bar *t* | **filtered** posterior at *t−1* | IS-fit, IS-decode |
| Transition lag | paid honestly via filtered lag | n/a |
| Kill switch | >3 flips / 10d → ×0.5 | n/a |

No look-ahead: regime for trading bar *t* uses `predict_proba` over data **up to
*t−1*** (filtered, last row). The filtered posterior naturally lags a true regime
change — this is the "5–15 day transition lag" cost, paid rather than assumed away.

## §1 HMM regime quality — the crux

Per-state annualised mean return / annualised vol / fraction of bars:

| Instrument | bull | neutral | bear | ret spread (bull−bear) | vol separation |
|---|---|---|---|---|---|
| BTC | +0.83 / 0.54 / 0.21 | −0.13 / 0.51 / 0.30 | +0.31 / 0.50 / 0.27 | +0.96 (**bear > neutral — not monotone**) | none (0.54≈0.51≈0.50) |
| ETH | +0.09 / 0.67 / 0.17 | +0.25 / 0.68 / 0.39 | +0.10 / 0.65 / 0.22 | +0.16 (**degenerate: neutral highest**) | none |
| SOL | +2.57 / 1.08 / 0.13 | +0.04 / 0.85 / 0.32 | +0.46 / 0.76 / 0.28 | +2.54 (**bull cleanly separates**) | yes (1.08 / 0.85 / 0.76) |

**Verdict on regime quality:** BTC and ETH regimes barely separate in return and
**not at all in vol** — the 3-state HMM effectively collapses to one regime, exactly
the degeneration R4 warned about for crypto. Only **SOL** shows a clean,
well-separated bull state with monotone vol.

Median regime detection lag: **BTC 11d, ETH 8d, SOL 5d** — squarely in the paper's
5–15 day band. Kill-switch active 14.6% / 21.6% / 13.2% of bars (ETH's high rate
reflects its unstable, non-separating regimes).

## §2 Gate results — combo vs solo legs vs naive blend

Walk-forward CPCV (n_splits=6, embargo=50, 10bps RT). `foldStd` = std of OOS Sharpe
across folds (the variance the thesis claims the combo should *reduce*).
`adjDSR` = DSR − selection-bias bar (1.387). PASS needs DSR>0, PBO<0.5, adjDSR>0.

| Instrument | variant | gross | meanOOS | foldStd | DSR | adjDSR | PBO | verdict |
|---|---|---|---|---|---|---|---|---|
| BTC | trend_solo | 0.119 | 0.309 | 1.245 | −0.936 | −2.323 | 0.33 | FAIL |
| BTC | mr_solo | 0.675 | −1.674 | 1.463 | −3.137 | −4.524 | 1.00 | FAIL |
| BTC | naive_blend | 0.681 | −1.042 | 1.651 | −2.693 | −4.079 | 0.83 | FAIL |
| BTC | **regime_combo** | 0.372 | −1.251 | 2.299 | −3.550 | −4.937 | 0.83 | **FAIL** |
| ETH | trend_solo | 0.202 | 0.553 | 1.492 | −0.939 | −2.326 | 0.50 | FAIL |
| ETH | mr_solo | 0.136 | −0.899 | 1.194 | −2.093 | −3.480 | 0.83 | FAIL |
| ETH | naive_blend | 0.310 | −0.325 | 2.042 | −2.367 | −3.754 | 0.50 | FAIL |
| ETH | **regime_combo** | 0.642 | 0.330 | 1.104 | −0.774 | −2.161 | 0.50 | **FAIL** |
| SOL | trend_solo | 0.270 | −0.121 | 1.659 | −1.780 | −3.167 | 0.67 | FAIL |
| SOL | mr_solo | 0.380 | 0.391 | 3.277 | −2.886 | −4.272 | 0.67 | FAIL |
| SOL | naive_blend | 0.539 | 0.045 | 2.389 | −2.345 | −3.731 | 0.67 | FAIL |
| SOL | **regime_combo** | 0.833 | **1.783** | 1.413 | **+0.370** | −1.017 | **0.17** | **FAIL** |

### Core claim: did combining lower fold variance / beat the best leg?

Mean across instruments:

| variant | mean foldStd (↓ better) | mean DSR (↑ better) |
|---|---|---|
| trend_solo | **1.465** | **−1.219** |
| mr_solo | 1.978 | −2.705 |
| naive_blend | 2.027 | −2.468 |
| regime_combo | 1.605 | −1.318 |

**The thesis is falsified at the portfolio level.** The regime combo is *not*
smoother than the best single leg (trend_solo 1.465 < combo 1.605) and does *not*
beat it on DSR (−1.219 vs −1.318). It only beats the worse legs (MR, naive blend).

## §3 Selection-bias robustness (honest extra-DoF accounting)

The combo adds DoF (3 states × legs × soft weights). None are tuned on the data
(HMM hyper-params fixed a-priori, strategy params = live params, weights = the
posteriors). Even ignoring the global trial count, the combo clears raw DSR>0 on
**SOL only**; under the formal N=7 bar (and inflated N) it passes nowhere:

| N_eff | DSR bar | combo passes |
|---|---|---|
| 7 (formal) | 1.387 | 0/3 |
| 12 | 1.665 | 0/3 |
| 20 | 1.901 | 0/3 |

## §4 Verdict

**REGIME-COMBO OVERALL: FAIL** (registered as global trial #7).

**Honest conclusion — did the combination rescue the strategies?**

No, not at the portfolio level — but the failure is *diagnostic*, not flat:

1. **The bottleneck is regime quality, not the combination idea.** Where the HMM
   produced genuinely separated regimes (**SOL**: bull +2.57 vs neutral ~0, real
   vol separation), the combo produced the **only positive DSR in the entire run**
   (+0.370) and the **best PBO (0.17)** — far better than any SOL solo leg
   (all DSR < −1.7). The soft router *works when the regime signal is real*.

2. **Where regimes don't separate, the router adds noise.** On BTC the 3-state HMM
   is near-degenerate (no vol separation, bear-return > neutral-return), and the
   combo *increased* fold variance (2.299 vs trend's 1.245) — mixing legs roughly
   at random. ETH is the same story, though there the combo at least beat its own
   legs (DSR −0.774, lowest-variance ETH variant).

3. **"Single strategy only earns in one regime" is real, but soft-combining can't
   fix it when the regime classifier itself fails.** Crypto regimes — as R4 already
   showed and R11 confirms — do not separate the way SPY's do (paper Sharpe 1.22).
   The combo never clears the gate because two of three instruments have
   un-learnable regimes; SOL alone is suggestive but dies on the selection-bias bar.

**Recommendation:** the regime-router architecture is sound (SOL proves the
mechanism); the missing ingredient is a regime signal that actually separates in
crypto. Promising next steps that do *not* just re-roll the same dice: (a) a
cross-asset / macro regime feature (BTC-dominance, funding, total-mcap vol) rather
than per-asset returns; (b) test on SOL-like high-dispersion alts where regimes do
separate. Re-running the same per-asset return+vol HMM on BTC/ETH will keep failing.

---
*Generated by `ops/scripts/hmm_regime_combo_gate.py` — R11 milestone.*
