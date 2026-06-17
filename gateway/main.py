"""helivex api-gateway — FastAPI service exposing strategy, gate, backtest, paper, and audit endpoints.

Start:
    cd /home/soffy/projects/helivex
    source venv/bin/activate
    uvicorn gateway.main:app --host 0.0.0.0 --port 8765 --reload

All endpoints mirror HELIVEX_FRONTEND_REQUIREMENTS.md §7 so the frontend
can flip USE_MOCK=false against http://localhost:8765.
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml
from fastapi import Body, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

from gateway.deps import (
    DB_DSN,
    PROJECT_ROOT,
    STRATEGY_SIGNAL_PREFIX,
    STRATEGY_YAML_MAP,
    close_pool,
    get_pool,
    latest_verdict,
    load_trials,
)

app = FastAPI(title="helivex api-gateway", version="0.8.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ─── Lifecycle ────────────────────────────────────────────────────────────────

@app.on_event("startup")
async def _startup() -> None:
    await get_pool()


@app.on_event("shutdown")
async def _shutdown() -> None:
    await close_pool()


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _read_yaml(path: Path) -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


def _write_yaml(path: Path, data: dict) -> None:
    with open(path, "w") as f:
        yaml.dump(data, f, default_flow_style=False, sort_keys=False)


def _strategy_id_to_yaml(strategy_id: str) -> Path:
    if strategy_id not in STRATEGY_YAML_MAP:
        raise HTTPException(404, f"Unknown strategy: {strategy_id}")
    p = STRATEGY_YAML_MAP[strategy_id]
    if not p.exists():
        raise HTTPException(404, f"Config file not found: {p.name}")
    return p


def _detect_regime(closes: list[float], ma_period: int = 200) -> str:
    if len(closes) < ma_period:
        return "unknown"
    ma = sum(closes[-ma_period:]) / ma_period
    return "bull" if closes[-1] > ma else "bear"


STRATEGY_DISPLAY_NAMES = {
    "trend_dual":   "趋势双向 (Donchian 4H)",
    "vwap_mr_dual": "VWAP 均值回归 (1H)",
    "spot_trend":   "现货趋势 (日线)",
}


def _latest_gate_metrics(strategy_id: str) -> dict:
    data = load_trials()
    yaml_name = STRATEGY_YAML_MAP.get(strategy_id, Path(strategy_id)).stem
    for entry in reversed(data.get("history", [])):
        cfg_path = entry.get("config", "")
        if strategy_id in cfg_path or yaml_name in cfg_path:
            instruments = entry.get("metrics", {}).get("instruments", {})
            if instruments:
                dsrs = [v.get("dsr") for v in instruments.values() if v.get("dsr") is not None]
                pbos = [v.get("pbo") for v in instruments.values() if v.get("pbo") is not None]
                return {
                    "dsr": round(sum(dsrs) / len(dsrs), 4) if dsrs else None,
                    "pbo": round(sum(pbos) / len(pbos), 4) if pbos else None,
                }
    return {"dsr": None, "pbo": None}


def _action_direction(action: str) -> str:
    if action in ("enter_long", "exit_short"):
        return "long"
    if action in ("enter_short", "exit_long", "time_exit"):
        return "short"
    return "neutral"


def _row_to_signal_log(r: Any) -> dict:
    action = r["action"]
    direction = _action_direction(action)
    acted = action != "NEUTRAL"
    indic_raw: dict = json.loads(r["indicators"]) if r["indicators"] else {}
    indicator_values = [
        {"name": k, "value": v}
        for k, v in indic_raw.items()
        if k != "warmup"
    ]
    return {
        "time": r["ts"].isoformat(),
        "direction": direction,
        "strength": 0.75 if acted else 0.2,
        "acted": acted,
        "indicator_values": indicator_values,
        "instrument": r["instrument"],
        "action": action,
        "signal_price": r["signal_price"],
        "has_signature": bool(r["sig_b64"]),
        "tier": "GOLD" if r["sig_b64"] else "STANDARD",
    }


# ─── /strategies ──────────────────────────────────────────────────────────────

@app.get("/strategies")
async def get_strategies() -> list[dict]:
    """Return StrategyState-compatible list for all 3 strategies."""
    pool = await get_pool()
    result = []
    for sid, yaml_path in STRATEGY_YAML_MAP.items():
        cfg = _read_yaml(yaml_path) if yaml_path.exists() else {}
        verdict = latest_verdict(sid)
        gate_m = _latest_gate_metrics(sid)

        prefix = STRATEGY_SIGNAL_PREFIX.get(sid, sid)
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT COUNT(*) AS n FROM paper.signals WHERE strategy_id LIKE $1", prefix
            )
        n_signals = int(row["n"]) if row else 0

        sl = cfg.get("signal_logic", {})
        result.append({
            "strategy_id": sid,
            "name": STRATEGY_DISPLAY_NAMES.get(sid, sid),
            "mode": cfg.get("mode", "paper"),
            "regime": "unknown",
            "position": "空仓",
            "signals_today": n_signals,
            "indicators": [],
            "signal_logic": {
                "entry": str(sl.get("entry", "")),
                "exit": str(sl.get("exit", "")),
                "min_confluence": sl.get("min_confluence", 1),
                "direction_mode": sl.get("direction_mode", "dual"),
            },
            "gate": {
                "verdict": ("pass" if verdict == "PASS" else "fail") if verdict else "pending",
                "dsr": gate_m.get("dsr"),
                "pbo": gate_m.get("pbo"),
                "reason": verdict,
            },
            # Extra context fields (not in StrategyState type, ignored by frontend)
            "n_paper_signals": n_signals,
            "instruments": cfg.get("instruments", []),
            "timeframe": cfg.get("timeframe", ""),
            "config_path": str(yaml_path.relative_to(PROJECT_ROOT)),
        })
    return result


@app.get("/strategies/{strategy_id}/config")
async def get_strategy_config(strategy_id: str) -> dict:
    """Return full YAML config for a strategy."""
    path = _strategy_id_to_yaml(strategy_id)
    return _read_yaml(path)


@app.put("/strategies/{strategy_id}/config")
async def put_strategy_config(strategy_id: str, body: dict = Body(...)) -> dict:
    """Overwrite strategy YAML config. Validates required top-level keys."""
    path = _strategy_id_to_yaml(strategy_id)
    required = {"strategy", "timeframe", "signal_logic", "risk", "gate"}
    missing = required - body.keys()
    if missing:
        raise HTTPException(400, f"Missing required config keys: {missing}")
    _write_yaml(path, body)
    return {"ok": True, "path": str(path.relative_to(PROJECT_ROOT))}


# ─── /gate ────────────────────────────────────────────────────────────────────

@app.post("/gate/run")
async def post_gate_run(
    config: str = Query(..., description="Strategy ID or relative config path"),
    instrument: str | None = Query(None),
    quiet: bool = Query(False),
) -> dict:
    """Run strategy_gate on a config. Returns DSR, PBO, verdict, fold metrics."""
    # Resolve config path
    if config in STRATEGY_YAML_MAP:
        config_path = str(STRATEGY_YAML_MAP[config])
    else:
        config_path = str(PROJECT_ROOT / config)

    if not Path(config_path).exists():
        raise HTTPException(404, f"Config not found: {config_path}")

    # Import and run (gate imports platform modules via sys.path in deps.py)
    sys.path.insert(0, str(PROJECT_ROOT / "tools"))
    from strategy_gate import run_gate  # type: ignore

    try:
        result = await run_gate(config_path, instrument=instrument, verbose=not quiet)
    except Exception as exc:
        raise HTTPException(500, str(exc))
    return result


@app.get("/gate/trials")
async def get_gate_trials() -> dict:
    """Return the full .gate_trials.json history."""
    return load_trials()


# ─── /backtest ────────────────────────────────────────────────────────────────

@app.post("/backtest/run")
async def post_backtest_run(
    config: str = Query(..., description="Strategy ID or relative config path"),
    instrument: str | None = Query(None),
) -> dict:
    """Run full backtest: fetch data → signals → P&L array → fold stats → regime segmentation.

    Returns the gate verdict dict plus per-bar pnl array and regime labels.
    """
    if config in STRATEGY_YAML_MAP:
        config_path = str(STRATEGY_YAML_MAP[config])
    else:
        config_path = str(PROJECT_ROOT / config)

    if not Path(config_path).exists():
        raise HTTPException(404, f"Config not found: {config_path}")

    sys.path.insert(0, str(PROJECT_ROOT / "tools"))
    from strategy_gate import _fetch_ohlcv, _resample_ohlcv, _resample_to_1d, _signals_to_pnl, _walk_forward_gate  # type: ignore
    import yaml as _yaml
    import importlib

    cfg = _yaml.safe_load(open(config_path))
    strategy_name = cfg["strategy"]

    SMAP = {
        "trend_dual":   ("omodul.strategies.trend_dual",   "trend_dual"),
        "vwap_mr_dual": ("omodul.strategies.vwap_mr_dual", "vwap_mr_dual"),
        "spot_trend":   ("omodul.strategies.spot_trend",   "spot_trend"),
    }
    if strategy_name not in SMAP:
        raise HTTPException(400, f"Unknown strategy: {strategy_name}")

    instr = instrument or (cfg.get("instruments", ["BTC-USDT-SWAP"])[0])
    mod_name, fn_name = SMAP[strategy_name]
    mod = importlib.import_module(mod_name)
    strategy_fn = getattr(mod, fn_name)

    try:
        raw = await _fetch_ohlcv(instr, cfg["db_source"])
    except Exception as exc:
        raise HTTPException(500, f"DB fetch error: {exc}")

    if cfg.get("resample_to_1d"):
        ohlcv = _resample_to_1d(raw)
    elif cfg.get("resample_bars", 1) > 1:
        ohlcv = _resample_ohlcv(raw, cfg["resample_bars"])
    else:
        ohlcv = raw

    market_state = {
        "ohlcv": ohlcv,
        "instrument": instr,
        "current_positions": {},
        "capital_usd": 10000.0,
    }
    out = strategy_fn(market_state, cfg)
    signals = out["signals"]
    closes  = ohlcv["close"]
    cost    = out["cost_bps"]
    direction = cfg.get("signal_logic", {}).get("direction", "both")

    pnl = _signals_to_pnl(signals, closes, cost, direction=direction)

    gate_cfg   = cfg.get("gate", {})
    n_splits   = gate_cfg.get("n_splits", 6)
    embargo    = gate_cfg.get("embargo_bars", 50)
    pbo_thr    = gate_cfg.get("pbo_threshold", 0.5)
    is_daily   = cfg.get("resample_to_1d", False) or cfg.get("timeframe", "") == "1D"
    periods_py = 252 if is_daily else (6 * 252 if "1H" in cfg.get("timeframe", "") else 2 * 252)

    gate_result = _walk_forward_gate(pnl, n_splits, embargo, periods_py, pbo_thr)

    # Regime segmentation: split pnl by 200-bar SMA of closes
    closes_arr = list(closes)
    regime_labels: list[str] = []
    for i, c in enumerate(closes_arr):
        window = closes_arr[max(0, i - 200):i + 1]
        ma = sum(window) / len(window)
        regime_labels.append("bull" if c > ma else "bear")

    return {
        **gate_result,
        "instrument": instr,
        "n_bars": int(len(pnl)),
        "n_signals": int(out["n_signals"]),
        "pnl": [float(x) for x in pnl],
        "regime": regime_labels,
    }


# ─── /executions ──────────────────────────────────────────────────────────────

@app.get("/executions")
async def get_executions(
    strategy_id: str | None = Query(None),
    limit: int = Query(200),
) -> dict:
    """Return paper fills with slippage stats."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        where = "WHERE strategy_id=$1" if strategy_id else ""
        args  = [strategy_id, limit] if strategy_id else [limit]
        limit_placeholder = "$2" if strategy_id else "$1"

        fills = await conn.fetch(
            f"""SELECT id, ts, strategy_id, instrument, side, quantity,
                       signal_price, actual_fill_price, slippage_bps,
                       order_id, latency_ms, fill_type
                FROM paper.fills {where}
                ORDER BY ts DESC LIMIT {limit_placeholder}""",
            *args,
        )
        agg = await conn.fetch(
            f"""SELECT strategy_id,
                       COUNT(*) AS n_fills,
                       AVG(slippage_bps) AS mean_slippage_bps,
                       PERCENTILE_CONT(0.95) WITHIN GROUP (ORDER BY slippage_bps) AS p95_slippage_bps,
                       AVG(latency_ms) AS mean_latency_ms
                FROM paper.fills {where}
                GROUP BY strategy_id""",
            *(args[:-1]),
        )
        sig_counts = await conn.fetch(
            f"""SELECT strategy_id, COUNT(*) AS n_signals
                FROM paper.signals {where}
                GROUP BY strategy_id""",
            *(args[:-1]),
        )

    sig_by_strat = {r["strategy_id"]: r["n_signals"] for r in sig_counts}
    fidelity = []
    for r in agg:
        sid = r["strategy_id"]
        n_sigs = sig_by_strat.get(sid, 0)
        n_fills = r["n_fills"]
        fidelity.append({
            "strategy_id": sid,
            "n_signals": n_sigs,
            "n_fills": n_fills,
            "fill_rate": n_fills / n_sigs if n_sigs else None,
            "mean_slippage_bps": float(r["mean_slippage_bps"]) if r["mean_slippage_bps"] else None,
            "p95_slippage_bps": float(r["p95_slippage_bps"]) if r["p95_slippage_bps"] else None,
            "mean_latency_ms": float(r["mean_latency_ms"]) if r["mean_latency_ms"] else None,
        })

    return {
        "fidelity": fidelity,
        "fills": [dict(r) for r in fills],
    }


# ─── /pnl ─────────────────────────────────────────────────────────────────────

@app.get("/pnl")
async def get_pnl(
    strategy_id: str | None = Query(None),
    instrument: str | None = Query(None),
) -> dict:
    """Return cumulative paper P&L computed from fills, segmented by strategy/instrument."""
    pool = await get_pool()
    filters = []
    args: list[Any] = []
    if strategy_id:
        args.append(strategy_id)
        filters.append(f"strategy_id=${len(args)}")
    if instrument:
        args.append(instrument)
        filters.append(f"instrument=${len(args)}")
    where = ("WHERE " + " AND ".join(filters)) if filters else ""

    async with pool.acquire() as conn:
        fills = await conn.fetch(
            f"""SELECT ts, strategy_id, instrument, side, quantity,
                       signal_price, actual_fill_price, slippage_bps
                FROM paper.fills {where}
                ORDER BY ts ASC""",
            *args,
        )

    # Group by (strategy_id, instrument), compute naive mark-to-market P&L
    from collections import defaultdict
    groups: dict[tuple, list] = defaultdict(list)
    for r in fills:
        groups[(r["strategy_id"], r["instrument"])].append(r)

    series = {}
    for (sid, inst), rows in groups.items():
        cum = 0.0
        pts = []
        for r in rows:
            sign = 1 if r["side"] == "BUY" else -1
            slip = float(r["slippage_bps"] or 0) / 10000
            # cost of fill relative to signal price
            pnl = -sign * float(r["quantity"]) * float(r["actual_fill_price"]) * slip
            cum += pnl
            pts.append({"ts": r["ts"].isoformat(), "cum_pnl_usd": round(cum, 4)})
        series[f"{sid}/{inst}"] = pts

    return {"series": series}


# ─── /audit ───────────────────────────────────────────────────────────────────

@app.get("/audit/decisions")
async def get_audit_decisions(
    limit: int = Query(100),
    strategy_id: str | None = Query(None),
) -> list[dict]:
    """Return recent GOLD-signed signal decisions from paper.signals."""
    pool = await get_pool()
    where = "WHERE strategy_id=$1" if strategy_id else ""
    args  = [strategy_id, limit] if strategy_id else [limit]
    limit_ph = "$2" if strategy_id else "$1"
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"""SELECT id, ts, strategy_id, instrument, action,
                       signal_price, audit_record_id, fingerprint_hex, sig_b64
                FROM paper.signals {where}
                ORDER BY ts DESC LIMIT {limit_ph}""",
            *args,
        )
    return [
        {
            "id": r["id"],
            "ts": r["ts"].isoformat(),
            "strategy_id": r["strategy_id"],
            "instrument": r["instrument"],
            "action": r["action"],
            "signal_price": r["signal_price"],
            "audit_record_id": r["audit_record_id"],
            "fingerprint_hex": r["fingerprint_hex"],
            "has_signature": bool(r["sig_b64"]),
            "tier": "GOLD" if r["sig_b64"] else "STANDARD",
        }
        for r in rows
    ]


@app.get("/audit/event/{event_id}")
async def get_audit_event(event_id: int) -> dict:
    """Return a single paper.signals row with full signature data."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """SELECT id, ts, strategy_id, instrument, action,
                      signal_price, audit_record_id, fingerprint_hex, sig_b64
               FROM paper.signals WHERE id=$1""",
            event_id,
        )
    if not row:
        raise HTTPException(404, f"Event {event_id} not found")
    return {
        "id": row["id"],
        "ts": row["ts"].isoformat(),
        "strategy_id": row["strategy_id"],
        "instrument": row["instrument"],
        "action": row["action"],
        "signal_price": row["signal_price"],
        "audit_record_id": row["audit_record_id"],
        "fingerprint_hex": row["fingerprint_hex"],
        "sig_b64": row["sig_b64"],
        "tier": "GOLD" if row["sig_b64"] else "STANDARD",
    }


@app.post("/verify_signature")
async def verify_signature(body: dict = Body(...)) -> dict:
    """Verify an Ed25519 signature from an audit record.

    Body: {fingerprint_hex: str, sig_b64: str, public_key_b64: str}
    """
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        from cryptography.exceptions import InvalidSignature

        fp_hex   = body["fingerprint_hex"]
        sig_b64  = body["sig_b64"]
        pub_b64  = body.get("public_key_b64") or os.environ.get("HELIVEX_AUDIT_PUBLIC_KEY_B64", "")

        if not pub_b64:
            raise HTTPException(400, "No public_key_b64 provided and HELIVEX_AUDIT_PUBLIC_KEY_B64 not set")

        pub_bytes = base64.b64decode(pub_b64)
        sig_bytes = base64.b64decode(sig_b64)
        msg_bytes = bytes.fromhex(fp_hex)

        pub_key = Ed25519PublicKey.from_public_bytes(pub_bytes)
        pub_key.verify(sig_bytes, msg_bytes)
        return {"valid": True, "fingerprint_hex": fp_hex}
    except (KeyError, ValueError) as e:
        raise HTTPException(400, f"Bad request body: {e}")
    except Exception:
        from cryptography.exceptions import InvalidSignature
        return {"valid": False, "fingerprint_hex": body.get("fingerprint_hex", "")}


@app.get("/audit/chain/verify")
async def get_audit_chain_verify() -> dict:
    """Verify all paper.signals records have valid Ed25519 signatures.

    Returns per-record validity and a summary pass/fail.
    """
    pub_b64 = os.environ.get("HELIVEX_AUDIT_PUBLIC_KEY_B64", "")
    if not pub_b64:
        return {"ok": False, "reason": "HELIVEX_AUDIT_PUBLIC_KEY_B64 not configured", "records": []}

    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    pub_bytes = base64.b64decode(pub_b64)
    pub_key   = Ed25519PublicKey.from_public_bytes(pub_bytes)

    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, fingerprint_hex, sig_b64 FROM paper.signals ORDER BY id"
        )

    records = []
    n_valid = 0
    for r in rows:
        if not r["sig_b64"]:
            records.append({"id": r["id"], "valid": None, "tier": "STANDARD"})
            continue
        try:
            pub_key.verify(
                base64.b64decode(r["sig_b64"]),
                bytes.fromhex(r["fingerprint_hex"]),
            )
            records.append({"id": r["id"], "valid": True, "tier": "GOLD"})
            n_valid += 1
        except Exception:
            records.append({"id": r["id"], "valid": False, "tier": "GOLD"})

    gold = [r for r in records if r["tier"] == "GOLD"]
    return {
        "ok": all(r["valid"] for r in gold) if gold else True,
        "n_total": len(records),
        "n_gold": len(gold),
        "n_valid": n_valid,
        "records": records,
    }


@app.get("/anchors")
async def get_anchors() -> dict:
    """Return first and last GOLD-signed records as audit chain anchors."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        first = await conn.fetchrow(
            "SELECT id, ts, fingerprint_hex, sig_b64 FROM paper.signals WHERE sig_b64 != '' ORDER BY id ASC LIMIT 1"
        )
        last = await conn.fetchrow(
            "SELECT id, ts, fingerprint_hex, sig_b64 FROM paper.signals WHERE sig_b64 != '' ORDER BY id DESC LIMIT 1"
        )

    def _fmt(r: Any) -> dict | None:
        if not r:
            return None
        return {
            "id": r["id"],
            "ts": r["ts"].isoformat(),
            "fingerprint_hex": r["fingerprint_hex"],
            "sig_b64": r["sig_b64"],
        }

    return {"first": _fmt(first), "last": _fmt(last)}


# ─── /strategies/{id}/mode ────────────────────────────────────────────────────

@app.put("/strategies/{strategy_id}/mode")
async def put_strategy_mode(
    strategy_id: str,
    mode: str = Query(..., description="Target mode: backtest | paper | live"),
    force: bool = Query(False, description="Second confirm to bypass gate warning"),
) -> dict:
    """Switch strategy mode with gate protection.

    Switching to 'paper' or 'live' without a passing gate verdict returns 409
    and requires ?force=true for the second confirmation.
    """
    if mode not in ("backtest", "paper", "live"):
        raise HTTPException(400, f"Invalid mode: {mode}. Must be backtest|paper|live")

    path = _strategy_id_to_yaml(strategy_id)
    cfg  = _read_yaml(path)
    current_mode = cfg.get("mode", "backtest")

    if mode in ("paper", "live"):
        verdict = latest_verdict(strategy_id)
        if verdict != "PASS":
            if not force:
                return {
                    "ok": False,
                    "warning": True,
                    "current_mode": current_mode,
                    "requested_mode": mode,
                    "gate_verdict": verdict,
                    "message": (
                        f"Strategy '{strategy_id}' has not passed the gate "
                        f"(last verdict: {verdict or 'none'}). "
                        "Re-submit with ?force=true to override."
                    ),
                }

    cfg["mode"] = mode
    _write_yaml(path, cfg)
    return {
        "ok": True,
        "strategy_id": strategy_id,
        "previous_mode": current_mode,
        "mode": mode,
        "gate_bypassed": mode in ("paper", "live") and latest_verdict(strategy_id) != "PASS" and force,
    }


# ─── /paper/account ───────────────────────────────────────────────────────────

@app.get("/paper/account")
async def get_paper_account() -> dict:
    """Return OKX Demo paper account state + today's P&L from fills."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        fills = await conn.fetch(
            """SELECT side, quantity, signal_price, actual_fill_price, slippage_bps
               FROM paper.fills WHERE ts >= CURRENT_DATE"""
        )
    # OKX Demo base balance (USDT); non-USDT assets shown as USDT-equivalent = 0 for simplicity
    balance = 5000.0
    pnl_gross = 0.0
    pnl_net = 0.0
    open_positions = 0
    for f in fills:
        sign = 1.0 if f["side"] == "SELL" else -1.0
        if f["signal_price"] and f["signal_price"] > 0:
            raw_pnl = sign * float(f["quantity"]) * (float(f["actual_fill_price"]) - float(f["signal_price"]))
            slip_cost = abs(float(f["slippage_bps"] or 0)) / 10000 * float(f["quantity"]) * float(f["actual_fill_price"])
            pnl_gross += raw_pnl
            pnl_net += raw_pnl - slip_cost
        if f["side"] == "BUY":
            open_positions += 1
        elif f["side"] == "SELL":
            open_positions = max(0, open_positions - 1)
    return {
        "balance": balance + round(pnl_net, 2),
        "positions": open_positions,
        "pnl_today_gross": round(pnl_gross, 2),
        "pnl_today_net": round(pnl_net, 2),
    }


# ─── /strategies/{id}/positions ───────────────────────────────────────────────

def _prefix_for(strategy_id: str) -> str:
    if strategy_id not in STRATEGY_SIGNAL_PREFIX:
        raise HTTPException(404, f"Unknown strategy: {strategy_id}")
    return STRATEGY_SIGNAL_PREFIX[strategy_id]


@app.get("/strategies/{strategy_id}/positions")
async def get_strategy_positions(strategy_id: str) -> list:
    """Return net open positions as Position[] inferred from fill history."""
    prefix = _prefix_for(strategy_id)
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT instrument, side, SUM(quantity) AS qty, AVG(actual_fill_price) AS avg_px
               FROM paper.fills WHERE strategy_id LIKE $1
               GROUP BY instrument, side ORDER BY instrument""",
            prefix,
        )
    from collections import defaultdict
    net: dict[str, dict] = defaultdict(lambda: {"buy_qty": 0.0, "sell_qty": 0.0, "buy_px": 0.0, "sell_px": 0.0})
    for r in rows:
        inst = r["instrument"]
        if r["side"] == "BUY":
            net[inst]["buy_qty"] += float(r["qty"]); net[inst]["buy_px"] = float(r["avg_px"])
        else:
            net[inst]["sell_qty"] += float(r["qty"]); net[inst]["sell_px"] = float(r["avg_px"])

    positions = []
    for inst, d in net.items():
        net_qty = d["buy_qty"] - d["sell_qty"]
        if abs(net_qty) < 1e-9:
            continue
        side = "long" if net_qty > 0 else "short"
        avg_entry = d["buy_px"] if net_qty > 0 else d["sell_px"]
        positions.append({
            "instrument": inst,
            "side": side,
            "quantity": round(abs(net_qty), 8),
            "avg_entry_price": round(avg_entry, 4),
            "current_price": None,
            "unrealized_pnl": 0.0,
            "unrealized_pnl_pct": 0.0,
            "holding_duration": "—",
            "margin_used": None,
            "leverage": None,
            "liquidation_price": None,
        })
    return positions


# ─── /strategies/{id}/trades ──────────────────────────────────────────────────

@app.get("/strategies/{strategy_id}/trades")
async def get_strategy_trades(strategy_id: str) -> list:
    """Return Trade[] — empty until fill pairs accumulate."""
    _prefix_for(strategy_id)  # validate
    return []


# ─── /strategies/{id}/equity ──────────────────────────────────────────────────

@app.get("/strategies/{strategy_id}/equity")
async def get_strategy_equity(strategy_id: str) -> dict:
    """Return StrategyEquity with equity curve points (5000 base + cum P&L)."""
    prefix = _prefix_for(strategy_id)
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT ts, side, quantity, signal_price, actual_fill_price
               FROM paper.fills WHERE strategy_id LIKE $1 ORDER BY ts ASC""",
            prefix,
        )
    base = 5000.0
    cum = 0.0
    points = []
    for r in rows:
        sign = 1.0 if r["side"] == "SELL" else -1.0
        sp = float(r["signal_price"] or 0)
        fp = float(r["actual_fill_price"])
        if sp > 0:
            cum += sign * float(r["quantity"]) * (fp - sp)
        points.append({
            "date": r["ts"].isoformat(),
            "equity": round(base + cum, 2),
            "drawdown": 0.0,
            "realized_pnl": round(cum, 4),
        })
    if not points:
        points = [{"date": datetime.now(timezone.utc).isoformat(), "equity": base, "drawdown": 0.0, "realized_pnl": 0.0}]
    return {"points": points, "by_instrument": None}


# ─── /strategies/{id}/signals ─────────────────────────────────────────────────

@app.get("/strategies/{strategy_id}/signals")
async def get_strategy_signals(
    strategy_id: str,
    limit: int = Query(100),
) -> list:
    """Return SignalLog[] with indicator snapshots for a strategy."""
    prefix = _prefix_for(strategy_id)
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT ts, instrument, action, signal_price, sig_b64, indicators
               FROM paper.signals WHERE strategy_id LIKE $1
               ORDER BY ts DESC LIMIT $2""",
            prefix, limit,
        )
    return [_row_to_signal_log(r) for r in rows]


# ─── /strategies/{id}/stats ───────────────────────────────────────────────────

@app.get("/strategies/{strategy_id}/stats")
async def get_strategy_stats(strategy_id: str) -> dict:
    """Return StrategyStats — honest zeros until fills accumulate."""
    prefix = _prefix_for(strategy_id)
    pool = await get_pool()
    async with pool.acquire() as conn:
        fill_row = await conn.fetchrow(
            """SELECT COUNT(*) AS n_fills,
                      SUM(CASE WHEN side='SELL' THEN 1.0 ELSE -1.0 END
                          * quantity * (actual_fill_price - COALESCE(signal_price, actual_fill_price))
                      ) AS total_pnl
               FROM paper.fills WHERE strategy_id LIKE $1""",
            prefix,
        )
    n_fills = int(fill_row["n_fills"]) if fill_row else 0
    total_pnl = float(fill_row["total_pnl"]) if fill_row and fill_row["total_pnl"] else 0.0
    gate_m = _latest_gate_metrics(strategy_id)
    return {
        "total_trades": n_fills // 2,
        "win_rate": 0.0,
        "profit_factor": 0.0,
        "avg_holding": "—",
        "max_drawdown": 0.0,
        "forward_sharpe": 0.0,
        "total_pnl": round(total_pnl, 4),
        "backtest_oos_sharpe": None,
        "sample_sufficient": n_fills >= 30,
    }


# ─── /strategies/{id}/execution ───────────────────────────────────────────────

@app.get("/strategies/{strategy_id}/execution")
async def get_strategy_execution(
    strategy_id: str,
    limit: int = Query(200),
) -> dict:
    """Return StrategyExecution with fill list and slippage summary."""
    prefix = _prefix_for(strategy_id)
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT id, ts, instrument, signal_price, actual_fill_price, fill_type, slippage_bps
               FROM paper.fills WHERE strategy_id LIKE $1
               ORDER BY ts DESC LIMIT $2""",
            prefix, limit,
        )
        agg = await conn.fetchrow(
            """SELECT AVG(slippage_bps) AS mean_slip, MAX(slippage_bps) AS max_slip
               FROM paper.fills WHERE strategy_id LIKE $1""",
            prefix,
        )

    return {
        "fills": [
            {
                "fill_id": str(r["id"]),
                "time": r["ts"].isoformat(),
                "instrument": r["instrument"],
                "expected_price": float(r["signal_price"] or 0),
                "actual_price": float(r["actual_fill_price"]),
                "liquidity": r["fill_type"] or "taker",
            }
            for r in rows
        ],
        "avg_slippage_bps": round(float(agg["mean_slip"]), 4) if agg and agg["mean_slip"] else 0.0,
        "max_slippage_bps": round(float(agg["max_slip"]), 4) if agg and agg["max_slip"] else 0.0,
        "backtest_assumed_bps": 2,
    }


# ─── /portfolio/equity ────────────────────────────────────────────────────────

@app.get("/portfolio/equity")
async def get_portfolio_equity() -> dict:
    """Return PortfolioEquity: combined curve + per-strategy breakdown."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT ts, strategy_id, side, quantity, signal_price, actual_fill_price
               FROM paper.fills ORDER BY ts ASC"""
        )

    from collections import defaultdict
    by_strat_rows: dict[str, list] = defaultdict(list)
    for r in rows:
        by_strat_rows[r["strategy_id"]].append(r)

    BASE_PER_STRATEGY = 5000.0
    by_strategy = []
    combined_map: dict[str, float] = {}

    for sid, fills in by_strat_rows.items():
        cum = 0.0
        points = []
        for r in fills:
            sign = 1.0 if r["side"] == "SELL" else -1.0
            sp = float(r["signal_price"] or 0)
            fp = float(r["actual_fill_price"])
            if sp > 0:
                cum += sign * float(r["quantity"]) * (fp - sp)
            ts_str = r["ts"].isoformat()
            combined_map[ts_str] = combined_map.get(ts_str, 0.0) + cum
            points.append({"date": ts_str, "equity": round(BASE_PER_STRATEGY + cum, 2), "drawdown": 0.0})
        by_strategy.append({"strategy_id": sid, "points": points, "contribution_pct": 0.0})

    combined = [
        {"date": ts, "equity": round(BASE_PER_STRATEGY * 3 + v, 2), "drawdown": 0.0}
        for ts, v in sorted(combined_map.items())
    ]
    if not combined:
        now = datetime.now(timezone.utc).isoformat()
        combined = [{"date": now, "equity": BASE_PER_STRATEGY * 3, "drawdown": 0.0}]

    return {"combined": combined, "by_strategy": by_strategy}


# ─── /portfolio/correlation ───────────────────────────────────────────────────

@app.get("/portfolio/correlation")
async def get_portfolio_correlation() -> dict:
    """Return CorrelationMatrix {strategies, matrix: number[][]} from daily P&L."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT DATE(ts) AS day, strategy_id,
                      SUM(CASE WHEN side='SELL' THEN 1.0 ELSE -1.0 END
                          * quantity * (actual_fill_price - COALESCE(signal_price, actual_fill_price))
                      ) AS daily_pnl
               FROM paper.fills GROUP BY DATE(ts), strategy_id ORDER BY day""",
        )

    from collections import defaultdict
    daily: dict[str, dict[str, float]] = defaultdict(dict)
    strats: set[str] = set()
    for r in rows:
        daily[str(r["day"])][r["strategy_id"]] = float(r["daily_pnl"])
        strats.add(r["strategy_id"])

    strat_list = sorted(strats)
    if len(strat_list) < 2:
        return {"strategies": strat_list, "matrix": [[1.0]] if len(strat_list) == 1 else []}

    days = sorted(daily.keys())
    vecs = {s: [daily[d].get(s, 0.0) for d in days] for s in strat_list}

    def _corr(a: list[float], b: list[float]) -> float:
        n = len(a)
        if n < 2:
            return 1.0 if a == b else 0.0
        ma, mb = sum(a) / n, sum(b) / n
        num = sum((x - ma) * (y - mb) for x, y in zip(a, b))
        da = sum((x - ma) ** 2 for x in a) ** 0.5
        db = sum((y - mb) ** 2 for y in b) ** 0.5
        return round(num / (da * db), 4) if da > 1e-12 and db > 1e-12 else (1.0 if a == b else 0.0)

    matrix = [[_corr(vecs[s1], vecs[s2]) for s2 in strat_list] for s1 in strat_list]
    return {"strategies": strat_list, "matrix": matrix}


# ─── /portfolio/summary ───────────────────────────────────────────────────────

@app.get("/portfolio/summary")
async def get_portfolio_summary() -> dict:
    """Return PortfolioSummary with realized P&L and exposure."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        fill_rows = await conn.fetch(
            """SELECT instrument, side, quantity,
                      CASE WHEN side='SELL' THEN 1.0 ELSE -1.0 END
                          * quantity * (actual_fill_price - COALESCE(signal_price, actual_fill_price)) AS pnl
               FROM paper.fills"""
        )

    from collections import defaultdict
    net_exp: dict[str, float] = defaultdict(float)
    total_pnl = 0.0
    for r in fill_rows:
        sign = 1.0 if r["side"] == "BUY" else -1.0
        net_exp[r["instrument"]] += sign * float(r["quantity"])
        total_pnl += float(r["pnl"])

    net_exposure = [{"instrument": k, "net": round(v, 8)} for k, v in net_exp.items() if abs(v) > 1e-9]
    return {
        "total_positions": len(net_exposure),
        "total_unrealized_pnl": 0.0,
        "total_realized_pnl": round(total_pnl, 4),
        "net_exposure": net_exposure,
        "margin_used": 0.0,
        "available": 15000.0,
    }


# ─── /portfolio/kill ──────────────────────────────────────────────────────────

@app.post("/portfolio/kill")
async def post_portfolio_kill() -> dict:
    """Send SIGTERM to paper node → on_stop() closes all positions."""
    import signal as _signal
    import os as _os

    pid_file = "/tmp/helivex_paper_node.pid"
    try:
        pid = int(Path(pid_file).read_text().strip())
    except (FileNotFoundError, ValueError):
        return {"ok": False, "reason": "paper node PID file not found — node may not be running"}

    try:
        _os.kill(pid, 0)
    except ProcessLookupError:
        return {"ok": False, "reason": f"PID {pid} not found — node already stopped"}

    try:
        _os.kill(pid, _signal.SIGTERM)
        return {
            "ok": True,
            "pid": pid,
            "action": "SIGTERM sent — node will run on_stop() and close all positions",
            "next": "wait ~10s then restart via paper/start_all.sh",
        }
    except PermissionError:
        return {"ok": False, "reason": f"permission denied sending SIGTERM to PID {pid}"}


# ─── Health ───────────────────────────────────────────────────────────────────

@app.get("/health")
async def health() -> dict:
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.fetchval("SELECT 1")
    return {"ok": True, "ts": datetime.now(timezone.utc).isoformat()}
