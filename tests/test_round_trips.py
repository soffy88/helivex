"""Unit tests for the FIFO round-trip extraction behind /strategies/{id}/trades
and /stats (gateway.main._round_trips). Pure logic — no DB, CI-safe.

The key invariant: total realized P&L over a fill sequence is path-independent
(FIFO must agree with avg-cost), and each closed trade carries the right side,
prices, and signed P&L.
"""
from __future__ import annotations

from datetime import datetime

from gateway.main import _fmt_duration, _round_trips


def _fill(ts_min, side, qty, px):
    return {
        "ts": datetime(2026, 1, 1, 0, ts_min),
        "instrument": "BTC-USDT-SWAP",
        "side": side,
        "quantity": float(qty),
        "signal_price": float(px),
        "actual_fill_price": float(px),
    }


def test_simple_long_round_trip() -> None:
    trades = _round_trips([_fill(0, "BUY", 1, 100), _fill(1, "SELL", 1, 110)])
    assert len(trades) == 1
    t = trades[0]
    assert t["side"] == "long"
    assert t["entry_price"] == 100 and t["exit_price"] == 110
    assert t["quantity"] == 1.0
    assert t["realized_pnl"] == 10.0


def test_simple_short_round_trip() -> None:
    # short profits when price falls: SELL 100 -> BUY 90 = +10
    trades = _round_trips([_fill(0, "SELL", 1, 100), _fill(1, "BUY", 1, 90)])
    assert len(trades) == 1
    assert trades[0]["side"] == "short"
    assert trades[0]["realized_pnl"] == 10.0


def test_partial_close_emits_multiple_trades() -> None:
    trades = _round_trips([
        _fill(0, "BUY", 2, 100),
        _fill(1, "SELL", 1, 110),  # close 1 -> +10
        _fill(2, "SELL", 1, 120),  # close 1 -> +20
    ])
    assert len(trades) == 2
    assert [t["realized_pnl"] for t in trades] == [10.0, 20.0]


def test_flip_through_zero() -> None:
    # BUY 1@100, SELL 2@110 (closes long +10, opens short 1@110), BUY 1@105 (closes short +5)
    trades = _round_trips([
        _fill(0, "BUY", 1, 100),
        _fill(1, "SELL", 2, 110),
        _fill(2, "BUY", 1, 105),
    ])
    assert len(trades) == 2
    assert trades[0]["side"] == "long" and trades[0]["realized_pnl"] == 10.0
    assert trades[1]["side"] == "short" and trades[1]["realized_pnl"] == 5.0
    assert round(sum(t["realized_pnl"] for t in trades), 6) == 15.0


def test_open_position_yields_no_trade() -> None:
    assert _round_trips([_fill(0, "BUY", 1, 100)]) == []


def test_pnl_pct_and_duration() -> None:
    trades = _round_trips([_fill(0, "BUY", 1, 100), _fill(30, "SELL", 1, 110)])
    t = trades[0]
    assert round(t["realized_pnl_pct"], 4) == 10.0  # 10 on 100 notional
    assert t["holding_duration"] == "30m 0s"


def test_fmt_duration() -> None:
    from datetime import timedelta
    assert _fmt_duration(timedelta(seconds=45)) == "45s"
    assert _fmt_duration(timedelta(minutes=3, seconds=2)) == "3m 2s"
    assert _fmt_duration(timedelta(hours=2, minutes=5)) == "2h 5m"
