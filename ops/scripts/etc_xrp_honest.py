"""R3.3: ETC/XRP honest gate — expanding-window hedge + HMM regime gate.

Step 1 — Look-ahead check:
    Version B (R3.2) used full-sample hedge → data leakage.
    Fix: expanding-window hedge (each fold uses only bars BEFORE its test period).

Step 2 — HMM regime gate:
    Train 2-state HMM on IS spread features.
    OOS: infer regime bar-by-bar; skip trading in broken regime.

Step 3 — Final gate verdict: is ETC/XRP deployable?

No OKX API key required.

Usage:
    python ops/scripts/etc_xrp_honest.py [--quiet]
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

import asyncpg
import numpy as np

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

DB_DSN = "postgresql://helios:helios_dev_pass@localhost:5434/helivex"
STAT_ARB_ROUND_TRIP = 0.0002 * 4   # 8 bps
PERIODS_1H = 24 * 252

N_SPLITS = 5


# ── Data loading ──────────────────────────────────────────────────────────────
def load_etc_xrp() -> tuple[np.ndarray, np.ndarray]:
    async def _f():
        conn = await asyncpg.connect(DB_DSN)
        etc = await conn.fetch(
            "SELECT close FROM market_data.ohlcv_1h WHERE instrument='ETC-USDT'"
            " ORDER BY bar_close_ts ASC"
        )
        xrp = await conn.fetch(
            "SELECT close FROM market_data.ohlcv_1h WHERE instrument='XRP-USDT'"
            " ORDER BY bar_close_ts ASC"
        )
        await conn.close()
        return (np.array([float(r["close"]) for r in etc]),
                np.array([float(r["close"]) for r in xrp]))
    return asyncio.run(_f())


# ── OLS hedge ─────────────────────────────────────────────────────────────────
def ols_hedge(a: np.ndarray, b: np.ndarray) -> float:
    X = np.column_stack([a, np.ones(len(a))])
    return float(np.linalg.lstsq(X, b, rcond=None)[0][0])


# ── Bar returns ───────────────────────────────────────────────────────────────
def spread_bar_returns(
    a: np.ndarray, b: np.ndarray,
    hedge: float, mean_s: float, std_s: float,
    ref: float, allow_trade: np.ndarray | None = None,
    entry_z: float = 2.0, exit_z: float = 0.5,
) -> list[float]:
    """Per-bar % returns. allow_trade[i]=False → flat for bar i."""
    spread = b - hedge * a
    z = (spread - mean_s) / (std_s + 1e-12)
    pos = 0
    rets: list[float] = []
    for i in range(1, len(spread)):
        dspread = float(spread[i] - spread[i - 1])
        bar_ret = pos * dspread / ref
        zi_prev = float(z[i - 1])

        trade_ok = (allow_trade is None or bool(allow_trade[i - 1]))

        if pos == 0 and trade_ok:
            if zi_prev <= -entry_z:
                pos = 1
                bar_ret = pos * dspread / ref - STAT_ARB_ROUND_TRIP
            elif zi_prev >= entry_z:
                pos = -1
                bar_ret = pos * dspread / ref - STAT_ARB_ROUND_TRIP
        elif pos != 0:
            if abs(zi_prev) <= exit_z or not trade_ok:
                pos = 0  # exit (also exit if regime closes gate mid-trade)
        rets.append(bar_ret)
    return rets


def _sharpe(returns: list[float], periods: int = PERIODS_1H) -> float:
    arr = np.asarray(returns, dtype=float)
    if len(arr) < 2:
        return 0.0
    std = float(arr.std())
    return 0.0 if std < 1e-12 else float((arr.mean() / std) * np.sqrt(periods))


# ── Expanding-window hedge factory ────────────────────────────────────────────
def make_expanding_hedge_strategy(
    a_full: np.ndarray, b_full: np.ndarray,
    allow_trade_full: np.ndarray | None = None,
) -> tuple[callable, list[float]]:
    """Returns strategy_fn closure + pre-computed expanding hedges."""
    n = len(a_full)
    fold_size = n // N_SPLITS

    # Pre-compute: hedge for fold f = OLS on bars [0 .. f*fold_size)
    hedges: list[float | None] = []
    for f in range(N_SPLITS):
        test_start = f * fold_size
        if test_start < 120:
            hedges.append(None)   # not enough prior history → skip fold
        else:
            hedges.append(ols_hedge(a_full[:test_start], b_full[:test_start]))

    call_count = [0]

    def strategy_fn(train_data: list, test_data: list) -> dict:
        fold_idx = call_count[0]
        call_count[0] += 1

        hedge = hedges[fold_idx]
        if hedge is None:
            # Fold 0: no prior history — skip (honest: can't estimate without leakage)
            return {"sharpe": 0.0, "returns": [], "is_sharpe": 0.0, "n_trades": 0}

        # IS: spread params from training data only
        a_tr = np.array([d[0] for d in train_data])
        b_tr = np.array([d[1] for d in train_data])
        sp_tr = b_tr - hedge * a_tr
        mean_s = float(sp_tr.mean())
        std_s = float(sp_tr.std())
        ref = float(np.mean(np.abs(b_tr))) + 1e-8

        is_rets = spread_bar_returns(a_tr, b_tr, hedge, mean_s, std_s, ref)
        is_sr = _sharpe(is_rets)

        # OOS
        a_te = np.array([d[0] for d in test_data])
        b_te = np.array([d[1] for d in test_data])

        # Allow-trade mask for OOS (from regime gate, if provided)
        if allow_trade_full is not None:
            test_start = fold_idx * fold_size
            test_end = test_start + fold_size if fold_idx < N_SPLITS - 1 else n
            at = allow_trade_full[test_start:test_end]
            if len(at) != len(a_te):
                at = None
        else:
            at = None

        oos_rets = spread_bar_returns(a_te, b_te, hedge, mean_s, std_s, ref, allow_trade=at)
        oos_sr = _sharpe(oos_rets)
        n_trades = sum(1 for i in range(1, len(oos_rets))
                       if abs(oos_rets[i]) > STAT_ARB_ROUND_TRIP * 0.9)

        return {"sharpe": oos_sr, "returns": oos_rets,
                "is_sharpe": is_sr, "n_trades": n_trades}

    return strategy_fn, hedges


# ── Gate runner ───────────────────────────────────────────────────────────────
def run_gate(
    label: str,
    strategy_fn: callable,
    data: list,
    quiet: bool = False,
) -> dict:
    from omodul.backtest_gate import BacktestGateConfig, backtest_gate  # noqa: PLC0415

    config = BacktestGateConfig(
        strategy_name=f"ETC_XRP_{label}",
        n_splits=N_SPLITS,
        embargo=24,
        pbo_threshold=0.5,
        periods=PERIODS_1H,
    )
    gate = backtest_gate(strategy_fn, data, config=config)
    wf = gate["walk_forward_result"]
    is_srs = [fr.get("is_sharpe", fr.get("sharpe", 0.0)) for fr in wf["fold_results"]]
    n_trades_list = [fr.get("n_trades", 0) for fr in wf["fold_results"]]
    return {
        "label": label,
        "gate_status": gate["gate_status"],
        "mean_is_sharpe": round(float(np.mean(is_srs)), 4),
        "mean_oos_sharpe": round(gate["mean_oos_sharpe"], 4),
        "deflated_sharpe": round(gate["deflated_sharpe"], 4),
        "pbo": round(gate["pbo"], 4),
        "fail_reasons": gate["fail_reasons"],
        "fold_oos": [round(s, 4) for s in wf["oos_sharpes"]],
        "fold_n_trades": n_trades_list,
    }


# ── HMM regime gate ───────────────────────────────────────────────────────────
def build_hmm_features(a: np.ndarray, b: np.ndarray, hedge: float) -> np.ndarray:
    """2-feature matrix: [spread_level, spread_abs_change] for HMM."""
    spread = b - hedge * a
    # Normalise spread to zero-mean, unit-std
    spread_norm = (spread - spread.mean()) / (spread.std() + 1e-12)
    # |Δspread| as volatility proxy
    abs_change = np.abs(np.diff(spread_norm, prepend=spread_norm[0]))
    return np.column_stack([spread_norm, abs_change])


def fit_hmm_on_is(feats_is: np.ndarray) -> dict:
    from oskill.hmm_regime_detect import hmm_regime_detect  # noqa: PLC0415
    result = hmm_regime_detect(feats_is, n_regimes=2, random_state=42)
    return result["model"]


def decode_regime_oos(feats_oos: np.ndarray, model: dict) -> np.ndarray:
    from oskill.hmm_regime_detect import hmm_regime_detect  # noqa: PLC0415
    result = hmm_regime_detect(feats_oos, n_regimes=2, trained_model=model)
    return np.array(result["regimes"])


def identify_cointegrated_regime(model: dict, feats_is: np.ndarray) -> int:
    """Identify which HMM state corresponds to the cointegrated (low-vol) regime.

    The cointegrated regime has lower spread volatility (lower |Δspread|).
    We check the mean emission for feature 1 (|Δspread|) — lower = cointegrated.
    """
    means = np.array(model.get("means", [[0], [0]]))
    if means.shape[0] >= 2 and means.shape[1] >= 2:
        # Feature index 1 = |Δspread|: lower mean → lower volatility → cointegrated
        return int(np.argmin(means[:, 1]))
    return 0   # fallback


def build_allow_trade(
    a_full: np.ndarray, b_full: np.ndarray,
) -> tuple[np.ndarray, dict]:
    """Walk-forward regime: fit HMM on IS bars, decode OOS bars."""
    n = len(a_full)
    fold_size = n // N_SPLITS
    allow_trade = np.ones(n, dtype=bool)
    regime_info: dict = {}

    for f in range(N_SPLITS):
        test_start = f * fold_size
        test_end = test_start + fold_size if f < N_SPLITS - 1 else n

        if test_start < 120:
            # No prior history — leave allow_trade=True (conservative)
            regime_info[f] = {"skipped": True}
            continue

        # IS features: bars [0, test_start)
        hedge_is = ols_hedge(a_full[:test_start], b_full[:test_start])
        feats_is = build_hmm_features(a_full[:test_start], b_full[:test_start], hedge_is)

        model = fit_hmm_on_is(feats_is)
        coint_state = identify_cointegrated_regime(model, feats_is)

        # Decode OOS bars
        feats_oos = build_hmm_features(a_full[test_start:test_end],
                                       b_full[test_start:test_end], hedge_is)
        oos_regimes = decode_regime_oos(feats_oos, model)
        allow_oos = (oos_regimes == coint_state)

        allow_trade[test_start:test_end] = allow_oos

        n_coint = int(allow_oos.sum())
        n_total = len(allow_oos)
        model_means = np.array(model.get("means", [[0, 0], [0, 0]]))
        transmat = np.array(model.get("transmat", [[1, 0], [0, 1]]))

        regime_info[f] = {
            "coint_state": coint_state,
            "coint_frac": round(n_coint / n_total, 4) if n_total > 0 else 0.0,
            "model_means": model_means.tolist(),
            "transmat": transmat.tolist(),
        }

    return allow_trade, regime_info


# ── Verdict doc ───────────────────────────────────────────────────────────────
def write_verdict(
    gate_lookahead: dict,
    gate_expanding: dict,
    gate_regime: dict,
    regime_info: dict,
    hedges: list,
    allow_trade: np.ndarray,
    full_p: float,
) -> None:
    docs = Path(__file__).parent.parent.parent / "docs"
    docs.mkdir(exist_ok=True)

    def gfmt(g: dict) -> str:
        return "**PASS**" if g["gate_status"] == "passed" else "FAIL"

    n = len(allow_trade)
    coint_frac_overall = float(allow_trade.sum()) / n

    lines = [
        "# R3.3 ETC/XRP Honest Gate: No Look-Ahead + HMM Regime",
        "",
        f"> Full-sample cointegration p = {full_p:.6f} (strongly cointegrated over 12 months)",
        "",
        "## §1 Look-Ahead Audit",
        "",
        "**R3.2 Version B leak**: full-sample hedge estimated from all 8640 bars",
        "including each fold's OOS period → data leakage.",
        "",
        "**Fix**: expanding-window hedge — each fold uses only bars BEFORE its test period.",
        "",
        "| Fold | Test start bar | Expanding hedge | Full-sample hedge (leaked) |",
        "|---|---|---|---|",
    ]
    fold_size = n // N_SPLITS
    for f in range(N_SPLITS):
        test_start = f * fold_size
        h = hedges[f]
        h_str = f"{h:.6f}" if h is not None else "N/A (skipped—no prior history)"
        lines.append(f"| {f} | {test_start} | {h_str} | 0.128928 |")

    lines += [
        "",
        "### Gate with Expanding Hedge (no look-ahead)",
        "",
        "| Metric | B: Fixed full-sample (leaked) | C: Expanding (honest) |",
        "|---|---|---|",
        f"| IS Sharpe | 2.6492 | {gate_expanding['mean_is_sharpe']:.4f} |",
        f"| OOS Sharpe | 2.0390 | {gate_expanding['mean_oos_sharpe']:.4f} |",
        f"| DSR | 0.8464 | {gate_expanding['deflated_sharpe']:.4f} |",
        f"| PBO | 0.2500 | {gate_expanding['pbo']:.4f} |",
        f"| Gate | PASS | {gfmt(gate_expanding)} |",
        f"| Fold OOS Sharpes | [4.01, 4.61, 1.58, 0.0, 0.0] | {gate_expanding['fold_oos']} |",
        "",
    ]

    if gate_expanding["gate_status"] == "passed":
        lines += ["**Expanding hedge still PASSES — R3.2 result was NOT fabricated by leakage.**", ""]
    else:
        lines += [
            "**Expanding hedge FAILS — R3.2 pass was partially due to look-ahead leakage.**",
            "",
            f"DSR dropped from 0.8464 → {gate_expanding['deflated_sharpe']:.4f}, "
            f"PBO shifted from 0.25 → {gate_expanding['pbo']:.4f}.",
            "",
        ]

    lines += [
        "## §2 HMM Regime Gate",
        "",
        "Gaussian HMM (2 states) trained on IS spread features:",
        "  - Feature 0: normalized spread level (spread z-score)",
        "  - Feature 1: |Δspread| (spread volatility proxy)",
        "",
        "Cointegrated state = lower spread volatility (|Δspread| mean).",
        "Broken state = higher spread volatility (spread trending/noisy).",
        "",
        f"Overall cointegrated fraction (all OOS bars): **{coint_frac_overall:.1%}**",
        "",
        "| Fold | Cointegrated state | Coint fraction | Status |",
        "|---|---|---|---|",
    ]
    for f, info in regime_info.items():
        if info.get("skipped"):
            lines.append(f"| {f} | N/A | N/A | skipped (no prior history) |")
        else:
            lines.append(
                f"| {f} | {info['coint_state']} | "
                f"{info['coint_frac']:.1%} | OK |"
            )

    lines += [
        "",
        "### Gate with HMM Regime Filter",
        "",
        "Only trade in cointegrated regime (broken regime → flat).",
        "",
        "| Metric | C: Expanding hedge | D: Expanding + HMM regime gate |",
        "|---|---|---|",
        f"| IS Sharpe | {gate_expanding['mean_is_sharpe']:.4f} | {gate_regime['mean_is_sharpe']:.4f} |",
        f"| OOS Sharpe | {gate_expanding['mean_oos_sharpe']:.4f} | {gate_regime['mean_oos_sharpe']:.4f} |",
        f"| DSR | {gate_expanding['deflated_sharpe']:.4f} | {gate_regime['deflated_sharpe']:.4f} |",
        f"| PBO | {gate_expanding['pbo']:.4f} | {gate_regime['pbo']:.4f} |",
        f"| Gate | {gfmt(gate_expanding)} | {gfmt(gate_regime)} |",
        f"| Fold OOS Sharpes | {gate_expanding['fold_oos']} | {gate_regime['fold_oos']} |",
        "",
    ]

    lines += ["## §3 Final Verdict", ""]

    expanding_pass = gate_expanding["gate_status"] == "passed"
    regime_pass = gate_regime["gate_status"] == "passed"

    if expanding_pass and regime_pass:
        lines += [
            "**ETC/XRP PASSES both the honest expanding-hedge gate AND the HMM regime gate.**",
            "",
            "**GO — genuine deployable alpha candidate** (subject to:",
            f"  rolling cointegration 46% stable; HMM cointegrated fraction {coint_frac_overall:.1%};",
            "  requires regime monitoring before live; start small).",
        ]
    elif expanding_pass and not regime_pass:
        lines += [
            "**ETC/XRP passes the honest gate but regime gate alters OOS Sharpe.**",
            "",
            f"Expanding-hedge gate: {gfmt(gate_expanding)} (DSR={gate_expanding['deflated_sharpe']:.4f})",
            f"With regime gate: {gfmt(gate_regime)} (DSR={gate_regime['deflated_sharpe']:.4f})",
            "",
            "The regime gate changes which bars are traded but the final DSR/PBO result",
            f"is {gate_regime['gate_status']}. Cointegrated-regime fraction: {coint_frac_overall:.1%}.",
            "Recommendation: conditional GO — trade only when HMM shows cointegrated regime.",
        ]
    elif not expanding_pass:
        lines += [
            "**ETC/XRP fails the honest (no look-ahead) gate.**",
            "",
            "R3.2 PASS was artefact of look-ahead in the fixed hedge.",
            f"Honest DSR = {gate_expanding['deflated_sharpe']:.4f}.",
            "Recommendation: NO-GO. Edge does not survive look-ahead removal.",
        ]

    # Current regime (last fold = most recent)
    last_fold_info = regime_info.get(N_SPLITS - 1, {})
    last_coint_frac = last_fold_info.get("coint_frac", 0.0)
    coint_state_last = last_fold_info.get("coint_state", "?")
    lines += [
        "",
        "## §4 Current Regime (2026-06)",
        "",
        f"Last fold (fold 4) HMM cointegrated fraction: **{last_coint_frac:.1%}**",
        f"(HMM state {coint_state_last} = cointegrated in that fold's model).",
        "",
        "Rolling cointegration in most recent 90-day windows (from R3.2): p > 0.5.",
        "Interpretation: ETC/XRP cointegration has broken in recent months (Feb–Jun 2026).",
        "**Current regime: BROKEN** → HMM gate would be CLOSED for live trading right now.",
        "",
        "---",
        "*Generated by `ops/scripts/etc_xrp_honest.py` — R3.3 milestone.*",
    ]

    (docs / "R3.3_ETC_XRP_HONEST.md").write_text("\n".join(lines) + "\n")


# ── Main ──────────────────────────────────────────────────────────────────────
def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()
    q = args.quiet

    if not q:
        print("=== R3.3 ETC/XRP Honest Gate ===\n")

    a_full, b_full = load_etc_xrp()
    n = len(a_full)
    fold_size = n // N_SPLITS
    data_full = list(zip(a_full.tolist(), b_full.tolist()))

    from oprim.cointegration_test import cointegration_test  # noqa: PLC0415
    full_p = float(cointegration_test(a_full, b_full)["p_value"])
    if not q:
        print(f"n={n}, fold_size={fold_size}, full coint p={full_p:.6f}\n")

    # ── §1a: expanding hedge gate (no look-ahead) ─────────────────────────────
    if not q:
        print("[1/3] Expanding-window hedge gate (honest, no look-ahead)...")
    fn_exp, hedges = make_expanding_hedge_strategy(a_full, b_full)
    if not q:
        for f, h in enumerate(hedges):
            h_str = f"{h:.6f}" if h else "SKIP"
            print(f"  Fold {f}: test_start={f*fold_size:5d}  hedge={h_str}")

    gate_expanding = run_gate("expanding_hedge", fn_exp, data_full, q)
    print(f"  Expanding: IS={gate_expanding['mean_is_sharpe']:+.4f}  "
          f"OOS={gate_expanding['mean_oos_sharpe']:+.4f}  "
          f"DSR={gate_expanding['deflated_sharpe']:+.4f}  "
          f"PBO={gate_expanding['pbo']:.4f}  "
          f"→ {gate_expanding['gate_status'].upper()}")

    # ── §2: HMM regime gate ──────────────────────────────────────────────────
    if not q:
        print("\n[2/3] Building HMM regime mask (walk-forward, IS fit → OOS decode)...")
    allow_trade, regime_info = build_allow_trade(a_full, b_full)

    coint_frac = float(allow_trade.sum()) / n
    if not q:
        for f, info in regime_info.items():
            if info.get("skipped"):
                print(f"  Fold {f}: SKIPPED (no prior history)")
            else:
                print(f"  Fold {f}: cointegrated_state={info['coint_state']}  "
                      f"coint_frac={info['coint_frac']:.1%}")
        print(f"  Overall cointegrated fraction: {coint_frac:.1%}")

    # ── §3: expanding hedge + regime gate ────────────────────────────────────
    if not q:
        print("\n[3/3] Expanding hedge + HMM regime gate combined...")
    fn_regime, _ = make_expanding_hedge_strategy(a_full, b_full, allow_trade_full=allow_trade)
    gate_regime = run_gate("expanding_hmm_regime", fn_regime, data_full, q)
    print(f"  Regime-gated: IS={gate_regime['mean_is_sharpe']:+.4f}  "
          f"OOS={gate_regime['mean_oos_sharpe']:+.4f}  "
          f"DSR={gate_regime['deflated_sharpe']:+.4f}  "
          f"PBO={gate_regime['pbo']:.4f}  "
          f"→ {gate_regime['gate_status'].upper()}")

    # ── Write verdict ─────────────────────────────────────────────────────────
    write_verdict(
        gate_lookahead={},  # placeholder (not used in doc — already in R3.2)
        gate_expanding=gate_expanding,
        gate_regime=gate_regime,
        regime_info=regime_info,
        hedges=hedges,
        allow_trade=allow_trade,
        full_p=full_p,
    )
    if not q:
        print("\ndocs/R3.3_ETC_XRP_HONEST.md written")

    # Key:value output for determinism
    print("\n=== R3.3 Key Results ===")
    print(f"  full_p                  : {full_p:.6f}")
    print(f"  exp_gate                : {gate_expanding['gate_status']}")
    print(f"  exp_oos_sharpe          : {gate_expanding['mean_oos_sharpe']}")
    print(f"  exp_dsr                 : {gate_expanding['deflated_sharpe']}")
    print(f"  exp_pbo                 : {gate_expanding['pbo']}")
    print(f"  hmm_coint_frac          : {coint_frac:.4f}")
    print(f"  regime_gate             : {gate_regime['gate_status']}")
    print(f"  regime_oos_sharpe       : {gate_regime['mean_oos_sharpe']}")
    print(f"  regime_dsr              : {gate_regime['deflated_sharpe']}")
    print(f"  regime_pbo              : {gate_regime['pbo']}")


if __name__ == "__main__":
    main()
