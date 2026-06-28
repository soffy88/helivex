"""tools.portfolio — portfolio construction scaffolding (correlation, vol-target,
risk-parity, fractional-Kelly allocation across strategies).

WHY THIS EXISTS NOW, WITH NOTHING TO ALLOCATE:
  13/13 gate trials FAIL — no strategy has positive OOS edge yet, and the paper
  book has ~0 real fills. So this allocator has no live alpha to combine *today*.
  It is scaffolding for the moment one or more strategies clear the gate: the whole
  point of running several weak-but-uncorrelated alphas is that the PORTFOLIO Sharpe
  can exceed any single one's. This module is the pure, tested machinery to do that
  combination, plus a demo that proves the diversification thesis on illustrative
  series when real ones are too sparse.

HOW IT CONNECTS TO THE RISK LAYER (paper/risk.py):
  The output is a set of target weights per strategy. Multiplying by the book's risk
  budget yields per-strategy target notional → which is exactly what feeds each
  strategy's qty_usd and the PER_STRATEGY_CAP in the risk gate. So: allocator decides
  *how much* of the budget each strategy gets; the risk gate enforces it as a hard cap.

Allocators (numpy-only, no SciPy dependency):
  - inverse_variance_weights(cov)         — 1/σ² normalised (min-variance-ish, no corr)
  - vol_target_weights(returns, tgt, ppy) — inverse-vol, scaled so portfolio σ ≈ target
  - risk_parity_weights(cov)              — equal risk contribution (iterative ERC)
  - fractional_kelly_weights(mu, cov, f)  — f·Σ⁻¹μ, long-only clipped & normalised

CLI:
  python -m tools.portfolio              # real series from paper.fills, else demo
  python -m tools.portfolio --demo       # force the illustrative 3-alpha demo
"""
from __future__ import annotations

import numpy as np

PERIODS_PER_YEAR_DAILY = 365.0


# ── core stats ──────────────────────────────────────────────────────────────────

def align_returns(series: dict[str, np.ndarray]) -> tuple[list[str], np.ndarray]:
    """Stack equal-length return series into (labels, matrix[T, N]). Truncates to the
    shortest series (caller should pass already-aligned series for real use)."""
    labels = list(series.keys())
    n = min(len(series[l]) for l in labels)
    mat = np.column_stack([np.asarray(series[l][-n:], dtype=float) for l in labels])
    return labels, mat


def sharpe(returns: np.ndarray, ppy: float = PERIODS_PER_YEAR_DAILY) -> float:
    r = np.asarray(returns, dtype=float)
    s = r.std()
    return float(r.mean() / s * np.sqrt(ppy)) if s > 1e-12 else 0.0


def cov_matrix(mat: np.ndarray) -> np.ndarray:
    return np.cov(mat, rowvar=False)


def corr_matrix(mat: np.ndarray) -> np.ndarray:
    return np.corrcoef(mat, rowvar=False)


# ── allocators ────────────────────────────────────────────────────────────────

def _normalise(w: np.ndarray) -> np.ndarray:
    w = np.clip(w, 0.0, None)
    s = w.sum()
    return w / s if s > 1e-12 else np.full_like(w, 1.0 / len(w))


def inverse_variance_weights(cov: np.ndarray) -> np.ndarray:
    var = np.clip(np.diag(cov), 1e-12, None)
    return _normalise(1.0 / var)


def vol_target_weights(mat: np.ndarray, target_vol_annual: float = 0.10,
                       ppy: float = PERIODS_PER_YEAR_DAILY) -> tuple[np.ndarray, float]:
    """Inverse-vol weights, then a single leverage scalar so the combined portfolio's
    annualised vol ≈ target. Returns (weights_summing_to_1, leverage)."""
    vol = np.clip(mat.std(axis=0), 1e-12, None)
    w = _normalise(1.0 / vol)
    port = mat @ w
    port_vol_annual = port.std() * np.sqrt(ppy)
    lev = target_vol_annual / port_vol_annual if port_vol_annual > 1e-12 else 1.0
    return w, float(lev)


def risk_parity_weights(cov: np.ndarray, iters: int = 10_000, tol: float = 1e-12) -> np.ndarray:
    """Equal-risk-contribution (ERC) long-only weights.

    Sqrt-damped multiplicative fixed point: w_i ← w_i·√(target/rc_i). The undamped
    1/marginal update is a period-2 map (x→k/x) that oscillates and collapses to a
    corner; the √ damping makes it converge to equal risk contributions."""
    n = cov.shape[0]
    w = np.full(n, 1.0 / n)
    for _ in range(iters):
        rc = w * (cov @ w)               # risk contribution per asset
        target = rc.mean()               # equal-budget target
        w_new = _normalise(w * np.sqrt(target / np.clip(rc, 1e-12, None)))
        if np.max(np.abs(w_new - w)) < tol:
            w = w_new
            break
        w = w_new
    return w


def fractional_kelly_weights(mu: np.ndarray, cov: np.ndarray, fraction: float = 0.25) -> np.ndarray:
    """Long-only fractional-Kelly: f·Σ⁻¹μ, clipped ≥0 and normalised. `mu` and `cov`
    must be on the same period basis (e.g. per-bar). Fraction<1 is the standard
    de-risking of full Kelly's notorious aggressiveness."""
    try:
        raw = fraction * np.linalg.solve(cov + np.eye(len(mu)) * 1e-12, mu)
    except np.linalg.LinAlgError:
        raw = fraction * mu
    return _normalise(raw)


# ── reporting ───────────────────────────────────────────────────────────────────

def diversification_report(series: dict[str, np.ndarray],
                           ppy: float = PERIODS_PER_YEAR_DAILY) -> dict:
    """Combine per-strategy return series and compare portfolio Sharpe (under each
    allocator) against equal-weight and the best single strategy — the core thesis:
    several weak, uncorrelated alphas can beat any one of them."""
    labels, mat = align_returns(series)
    cov = cov_matrix(mat)
    corr = corr_matrix(mat)
    mu = mat.mean(axis=0)

    singles = {l: sharpe(mat[:, i], ppy) for i, l in enumerate(labels)}
    best_single = max(singles.values())

    allocs: dict[str, np.ndarray] = {
        "equal":      _normalise(np.ones(len(labels))),
        "inv_var":    inverse_variance_weights(cov),
        "risk_parity": risk_parity_weights(cov),
        "frac_kelly": fractional_kelly_weights(mu, cov, fraction=0.25),
    }
    vt_w, vt_lev = vol_target_weights(mat, target_vol_annual=0.10, ppy=ppy)
    allocs["vol_target_10%"] = vt_w

    port = {}
    for name, w in allocs.items():
        pr = mat @ w
        port[name] = {
            "weights": {l: round(float(w[i]), 4) for i, l in enumerate(labels)},
            "sharpe": round(sharpe(pr, ppy), 3),
        }
    port["vol_target_10%"]["leverage"] = round(vt_lev, 2)

    # mean pairwise correlation (off-diagonal)
    off = corr[~np.eye(len(labels), dtype=bool)]
    return {
        "labels": labels,
        "n_obs": mat.shape[0],
        "single_sharpes": {l: round(v, 3) for l, v in singles.items()},
        "best_single_sharpe": round(best_single, 3),
        "mean_pairwise_corr": round(float(off.mean()), 3),
        "corr_matrix": np.round(corr, 3).tolist(),
        "portfolios": port,
    }


def _print_report(rep: dict) -> None:
    print("── portfolio construction ──────────────────────────────────────")
    print(f"strategies      : {rep['labels']}")
    print(f"observations    : {rep['n_obs']}")
    print(f"single Sharpes  : {rep['single_sharpes']}")
    print(f"best single     : {rep['best_single_sharpe']}")
    print(f"mean pairwise ρ : {rep['mean_pairwise_corr']}")
    print("portfolio Sharpe by allocator (vs best single = "
          f"{rep['best_single_sharpe']}):")
    for name, p in rep["portfolios"].items():
        lev = f"  lev×{p['leverage']}" if "leverage" in p else ""
        flag = "  ◀ beats best single" if p["sharpe"] > rep["best_single_sharpe"] else ""
        print(f"  {name:16s} Sharpe={p['sharpe']:+.3f}{lev}{flag}")
        print(f"  {'':16s} weights={p['weights']}")


# ── data source: real per-strategy daily returns from paper.fills ───────────────

async def load_strategy_returns_from_fills(min_obs: int = 30) -> dict[str, np.ndarray]:
    """Build per-strategy daily realized-return series from paper.fills. Returns only
    strategies with >= min_obs daily observations (typically empty until the book
    trades materially — by design)."""
    import asyncpg

    from paper.db import DB_DSN
    from paper.risk import BASE_EQUITY_USD, realized_pnl  # reuse the matched-PnL walk

    conn = await asyncpg.connect(DB_DSN)
    try:
        strats = [r["strategy_id"] for r in await conn.fetch(
            "SELECT DISTINCT strategy_id FROM paper.fills")]
        # daily realized PnL per strategy via the same avg-cost matcher, day by day
        out: dict[str, np.ndarray] = {}
        for s in strats:
            days = [r["d"] for r in await conn.fetch(
                "SELECT DISTINCT date_trunc('day', ts) d FROM paper.fills "
                "WHERE strategy_id=$1 ORDER BY d", s)]
            if len(days) < min_obs:
                continue
            # cumulative realized up to end of each day → diff = daily PnL
            cum = []
            for d in days:
                end = d + __import__("datetime").timedelta(days=1)
                rows = await conn.fetch(
                    "SELECT strategy_id,instrument,side,quantity,actual_fill_price "
                    "FROM paper.fills WHERE strategy_id=$1 AND ts < $2 ORDER BY ts", s, end)
                cum.append(_matched_pnl(rows))
            daily = np.diff(np.array([0.0] + cum)) / BASE_EQUITY_USD
            out[s] = daily
        return out
    finally:
        await conn.close()


def _matched_pnl(rows) -> float:
    """avg-cost matched realized PnL over a fill row list (mirrors paper.risk)."""
    pos: dict[tuple[str, str], list[float]] = {}
    pnl = 0.0
    for r in rows:
        key = (r["strategy_id"], r["instrument"])
        q, cost = pos.get(key, [0.0, 0.0])
        fq = float(r["quantity"]) * (1.0 if r["side"] == "BUY" else -1.0)
        px = float(r["actual_fill_price"])
        if q == 0 or (q > 0) == (fq > 0):
            nq = q + fq
            cost = (cost * abs(q) + px * abs(fq)) / abs(nq) if nq != 0 else 0.0
            q = nq
        else:
            closed = min(abs(fq), abs(q))
            pnl += (1.0 if q > 0 else -1.0) * (px - cost) * closed
            q += fq
            cost = 0.0 if q == 0 else (px if (q > 0) != (q - fq > 0) else cost)
        pos[key] = [q, cost]
    return pnl


# ── illustrative demo (clearly labelled — NOT real strategy data) ───────────────

def _demo_series() -> dict[str, np.ndarray]:
    """Three deliberately WEAK alphas (single Sharpe ~0.4–0.6) with LOW mutual
    correlation, on a fixed seed. Demonstrates the thesis: combined Sharpe > best
    single purely from diversification. These numbers are synthetic, not helivex
    strategy results."""
    rng = np.random.default_rng(42)
    n = 365

    def _demean(x: np.ndarray) -> np.ndarray:
        return x - x.mean()

    # demean the shared factor and each idiosyncratic noise so the realized mean of
    # every series equals its drift exactly → single Sharpes are reproducible, not
    # at the mercy of a particular RNG draw.
    common = _demean(rng.standard_normal(n)) * 0.004   # small shared market factor
    a = 0.00035 + 0.5 * common + _demean(rng.standard_normal(n)) * 0.011
    b = 0.00030 - 0.4 * common + _demean(rng.standard_normal(n)) * 0.011
    c = 0.00040 + 0.2 * common + _demean(rng.standard_normal(n)) * 0.011
    return {"alpha_A": a, "alpha_B": b, "alpha_C": c}


def main() -> None:
    import sys
    if "--demo" in sys.argv:
        print("[demo] illustrative synthetic alphas — NOT helivex strategy data\n")
        _print_report(diversification_report(_demo_series()))
        return

    import asyncio
    real = asyncio.run(load_strategy_returns_from_fills(min_obs=30))
    if len(real) >= 2:
        print(f"[real] per-strategy daily returns from paper.fills ({len(real)} strategies)\n")
        _print_report(diversification_report(real))
    else:
        print("[real] insufficient paper.fills history for >=2 strategies "
              f"({len(real)} qualified) — 13/13 gate FAIL, book barely trades.\n"
              "Showing the illustrative demo instead (run --demo to force):\n")
        _print_report(diversification_report(_demo_series()))


if __name__ == "__main__":
    main()
