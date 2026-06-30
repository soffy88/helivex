"""Frontend↔gateway contract test.

The web app (helivex-web) consumes the gateway over HTTP with fixed TypeScript
shapes (src/types/api.ts, src/lib/api-client.ts). If an endpoint is renamed or
its shape drifts, the UI breaks silently at runtime — exactly the failure mode
behind the dead `verifySig` (client called /audit/verify_signature; gateway only
has POST /verify_signature). This asserts every endpoint the frontend calls
exists and returns the keys the UI reads.

Runs against a LIVE gateway (default :8765). Skips cleanly if it's not up, so it
never breaks a no-services CI run; run it with the stack up to get real coverage.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

import pytest

BASE = os.environ.get("HELIVEX_GW_BASE", "http://localhost:8765")


def _get(path: str):
    with urllib.request.urlopen(f"{BASE}{path}", timeout=5) as r:
        return json.loads(r.read())


def _gateway_up() -> bool:
    try:
        _get("/health")
        return True
    except (urllib.error.URLError, OSError, ValueError):
        return False


pytestmark = pytest.mark.skipif(
    not _gateway_up(), reason=f"gateway not reachable at {BASE} — start the stack to run contract tests"
)


def _assert_keys(obj: dict, keys: set[str], where: str) -> None:
    missing = keys - set(obj.keys())
    assert not missing, f"{where}: missing keys {missing} (have {sorted(obj.keys())})"


# ── global endpoints the dashboard reads ────────────────────────────────────────

def test_strategies_shape():
    data = _get("/strategies")
    assert isinstance(data, list) and data, "/strategies must return a non-empty list"
    _assert_keys(data[0], {"strategy_id", "name", "position", "signals_today"}, "/strategies[0]")


def test_paper_account_shape():
    _assert_keys(_get("/paper/account"),
                 {"balance", "pnl_today_net", "pnl_today_gross", "positions"}, "/paper/account")


def test_gate_trials_shape():
    d = _get("/gate/trials")
    _assert_keys(d, {"total_trials", "history"}, "/gate/trials")
    if d["history"]:
        _assert_keys(d["history"][0], {"trial_n", "config", "verdict", "metrics"}, "/gate/trials.history[0]")


def test_chain_verify_shape():
    _assert_keys(_get("/audit/chain/verify"), {"ok", "n_total", "n_gold"}, "/audit/chain/verify")


def test_executions_shape():
    _assert_keys(_get("/executions"), {"fidelity", "fills"}, "/executions")


def test_audit_decisions_is_list():
    assert isinstance(_get("/audit/decisions"), list)


# ── portfolio ────────────────────────────────────────────────────────────────

def test_portfolio_summary_shape():
    _assert_keys(_get("/portfolio/summary"),
                 {"total_positions", "total_realized_pnl", "available"}, "/portfolio/summary")


def test_portfolio_equity_shape():
    _assert_keys(_get("/portfolio/equity"), {"combined"}, "/portfolio/equity")


def test_portfolio_correlation_shape():
    _assert_keys(_get("/portfolio/correlation"), {"strategies", "matrix"}, "/portfolio/correlation")


# ── R14 risk + R16 microstructure (new panels) ──────────────────────────────────

def test_risk_status_shape():
    d = _get("/risk/status")
    _assert_keys(d, {"kill_switch", "nav", "drawdown_pct", "realized_today", "caps"}, "/risk/status")
    _assert_keys(d["kill_switch"], {"tripped", "reason"}, "/risk/status.kill_switch")
    _assert_keys(d["caps"], {"portfolio_gross_usd", "per_strategy_usd", "max_drawdown_pct",
                             "daily_loss_limit_usd"}, "/risk/status.caps")


def test_risk_events_is_list():
    d = _get("/risk/events?limit=5")
    assert isinstance(d, list)
    if d:
        _assert_keys(d[0], {"ts", "kind", "severity", "message"}, "/risk/events[0]")


def test_microstructure_shape():
    d = _get("/microstructure/latest?series=5")
    _assert_keys(d, {"latest", "series"}, "/microstructure/latest")
    if d["latest"]:
        _assert_keys(d["latest"][0],
                     {"instrument", "mid", "spread_bps", "microprice", "imbalance1", "imbalance5"},
                     "/microstructure/latest.latest[0]")


# ── per-strategy detail endpoints (id pulled live) ──────────────────────────────

def test_strategy_detail_endpoints():
    sid = _get("/strategies")[0]["strategy_id"]
    # these must not 404 for a real strategy id; shapes are list/dict per the UI
    assert isinstance(_get(f"/strategies/{sid}/positions"), list)
    assert isinstance(_get(f"/strategies/{sid}/trades"), list)
    _assert_keys(_get(f"/strategies/{sid}/equity"), {"points"}, "equity")
    assert isinstance(_get(f"/strategies/{sid}/signals"), list)
    _assert_keys(_get(f"/strategies/{sid}/stats"), {"total_trades"}, "stats")
    _assert_keys(_get(f"/strategies/{sid}/execution"), {"fills"}, "execution")
