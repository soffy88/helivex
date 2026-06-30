"""Unit tests for the portfolio risk gate (paper/risk.py) and the allocation
scaffolding (tools/portfolio.py). Pure logic only — no DB, CI-safe."""
from __future__ import annotations

import numpy as np
import pytest

from paper.risk import (
    PER_INSTRUMENT_CAP,
    PORTFOLIO_GROSS_CAP,
    PortfolioRiskManager,
    is_tripped,
    reset,
    trip,
)
from tools.portfolio import (
    diversification_report,
    fractional_kelly_weights,
    risk_parity_weights,
    _demo_series,
)


# ── pre-trade gate ──────────────────────────────────────────────────────────────

def test_gate_allows_within_caps() -> None:
    m = PortfolioRiskManager()
    assert m.gate_entry("s1", "BTC-USDT-SWAP", 200).allowed


def test_gate_blocks_instrument_concentration() -> None:
    m = PortfolioRiskManager()
    filled = 0.0
    s = 0
    while filled + 200 <= PER_INSTRUMENT_CAP:
        m.open_position(f"s{s}", "BTC-USDT-SWAP", 200)
        filled += 200
        s += 1
    d = m.gate_entry(f"s{s}", "BTC-USDT-SWAP", 200)  # one more breaches per-instrument cap
    assert not d.allowed
    assert "instrument" in d.reason


def test_gate_blocks_portfolio_gross() -> None:
    m = PortfolioRiskManager()
    # spread across instruments so per-instrument cap isn't the binding constraint
    per = 100.0
    k = 0
    while m.gross_exposure() + per <= PORTFOLIO_GROSS_CAP:
        m.open_position(f"s{k}", f"INST{k}", per)
        k += 1
    d = m.gate_entry(f"s{k}", f"INST{k}", per)
    assert not d.allowed
    assert "gross" in d.reason


def test_reentry_replaces_not_adds() -> None:
    m = PortfolioRiskManager()
    m.open_position("s1", "BTC-USDT-SWAP", 200)
    m.open_position("s1", "BTC-USDT-SWAP", 200)  # same key — replace
    assert m.instrument_exposure("BTC-USDT-SWAP") == 200


def test_kill_switch_blocks_entries_allows_after_reset() -> None:
    m = PortfolioRiskManager()
    reset()
    trip("unit test")
    try:
        assert is_tripped()
        assert not m.gate_entry("s1", "ETH-USDT-SWAP", 50).allowed
    finally:
        reset()
    assert m.gate_entry("s1", "ETH-USDT-SWAP", 50).allowed


def test_close_position_frees_exposure() -> None:
    m = PortfolioRiskManager()
    m.open_position("s1", "BTC-USDT-SWAP", 200)
    m.close_position("s1", "BTC-USDT-SWAP")
    assert m.gross_exposure() == 0


# ── allocators ──────────────────────────────────────────────────────────────────

def test_erc_equalises_risk_contributions() -> None:
    cov = np.array([[0.04, 0.006, 0.0],
                    [0.006, 0.09, 0.01],
                    [0.0, 0.01, 0.16]])
    w = risk_parity_weights(cov)
    rc = w * (cov @ w)
    rc = rc / rc.sum()
    assert np.allclose(rc, 1.0 / 3, atol=1e-4)
    assert abs(w.sum() - 1.0) < 1e-9
    assert (w >= 0).all()


def test_fractional_kelly_is_long_only_normalised() -> None:
    mu = np.array([0.001, -0.002, 0.0005])  # one negative-edge strategy
    cov = np.eye(3) * 0.01
    w = fractional_kelly_weights(mu, cov, fraction=0.25)
    assert (w >= 0).all()                 # long-only clip
    assert abs(w.sum() - 1.0) < 1e-9
    assert w[1] == 0.0                     # negative-edge strategy gets zero weight


def test_diversification_beats_best_single() -> None:
    """The core thesis: weak, low-correlation alphas combine to a higher Sharpe than
    any single one. Equal-weight (return-agnostic) must clear best-single here."""
    rep = diversification_report(_demo_series())
    assert rep["mean_pairwise_corr"] < 0.2
    assert rep["portfolios"]["equal"]["sharpe"] > rep["best_single_sharpe"]


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
