"""Focused tests for the real overfitting statistics in tools/strategy_gate.py.

Covers:
  - _cscv_pbo: the real CSCV logit-rank Probability of Backtest Overfitting
    (López de Prado 2014) — discrimination, determinism, bounds, short-series
    guard.
  - _walk_forward_gate label PURGING: the IS tail is dropped by purge_bars so
    multi-bar labels can't leak into the OOS window.

No DB / no network; pure-numpy series only.
"""
from __future__ import annotations

import math

import numpy as np

from tools.strategy_gate import _cscv_pbo, _walk_forward_gate


def test_cscv_pbo_bounds_and_determinism() -> None:
    rng = np.random.default_rng(0)
    noise = rng.standard_normal(2000)
    p1 = _cscv_pbo(noise)
    p2 = _cscv_pbo(noise)
    assert p1 == p2, "CSCV PBO must be deterministic (fixed seed)"
    assert 0.0 <= p1 <= 1.0


def test_cscv_pbo_short_series_is_nan() -> None:
    # Fewer than n_blocks*10 observations → undefined.
    assert math.isnan(_cscv_pbo(np.zeros(50)))


def test_cscv_pbo_discriminates_luck_concentration() -> None:
    """A luck-concentrated 'edge' must score MORE overfit than an evenly-spread
    one — on AVERAGE over seeds.

    The self-surrogate CSCV-PBO of a single config is a noisy statistic (a
    documented limitation: reshuffles of a uniformly-distributed P&L are nearly
    identical to it, so any single seed can land well off 0.5). The robust,
    defensible signal is the directional gap between a win concentrated in a few
    blocks (fails OOS → high PBO) and one spread evenly across all blocks
    (~0.5), averaged across independent draws.
    """
    def mean_pbo(make) -> float:
        return float(np.mean([_cscv_pbo(make(s)) for s in range(15)]))

    even = mean_pbo(
        lambda s: 0.001 + 0.001 * np.random.default_rng(s).standard_normal(2000)
    )

    def _luck(s: int) -> np.ndarray:
        x = np.random.default_rng(s).standard_normal(2000).copy()
        x[:500] += 0.5
        return x

    luck = mean_pbo(_luck)
    assert luck > even + 0.1, f"expected luck≫even, got luck={luck:.3f} even={even:.3f}"
    assert luck >= 0.6   # a luck-concentrated 'win' is flagged overfit on average


def test_walk_forward_purge_shortens_is_segment() -> None:
    """Purging drops the IS tail: with purge_bars>0 the IS Sharpes must differ
    from the no-purge run on a series where the IS tail carries signal."""
    rng = np.random.default_rng(2)
    pnl = 0.0005 + 0.01 * rng.standard_normal(6000)
    ppy = 2190
    g_purged = _walk_forward_gate(pnl, 6, 50, ppy, 0.5, purge_bars=40)
    g_plain = _walk_forward_gate(pnl, 6, 50, ppy, 0.5, purge_bars=0)
    assert g_purged["purged_cv"] is True
    assert g_purged["purge_bars"] == 40
    assert g_plain["purge_bars"] == 0
    # Purge changes the IS windows → at least one fold's IS Sharpe must move.
    assert g_purged["is_sharpes"] != g_plain["is_sharpes"]
    # New real-PBO key is always present and within bounds (or NaN).
    pc = g_purged["pbo_cscv"]
    assert math.isnan(pc) or (0.0 <= pc <= 1.0)


def test_walk_forward_keeps_legacy_keys() -> None:
    """Backward-compat: the heuristic 'pbo' and 'deflated_sharpe' keys survive."""
    rng = np.random.default_rng(3)
    pnl = 0.01 * rng.standard_normal(6000)
    g = _walk_forward_gate(pnl, 6, 50, 2190, 0.5, purge_bars=20)
    for key in ("pbo", "deflated_sharpe", "pbo_cscv", "purge_bars", "purged_cv"):
        assert key in g
