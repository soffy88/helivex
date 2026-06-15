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


# ─── /strategies ──────────────────────────────────────────────────────────────

@app.get("/strategies")
async def get_strategies() -> list[dict]:
    """Return status + config summary + regime for all 3 strategies."""
    pool = await get_pool()
    result = []
    for sid, yaml_path in STRATEGY_YAML_MAP.items():
        cfg = _read_yaml(yaml_path) if yaml_path.exists() else {}
        verdict = latest_verdict(sid)

        # Signal count from paper DB — match by prefix (paper node uses instrument-qualified IDs)
        prefix = STRATEGY_SIGNAL_PREFIX.get(sid, sid)
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT COUNT(*) AS n FROM paper.signals WHERE strategy_id LIKE $1", prefix
            )
        n_signals = row["n"] if row else 0

        result.append({
            "id": sid,
            "strategy": cfg.get("strategy", sid),
            "timeframe": cfg.get("timeframe", ""),
            "instruments": cfg.get("instruments", []),
            "mode": cfg.get("mode", "backtest"),
            "last_gate_verdict": verdict,
            "gate_passed": verdict == "PASS",
            "n_paper_signals": n_signals,
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


# ─── Health ───────────────────────────────────────────────────────────────────

@app.get("/health")
async def health() -> dict:
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.fetchval("SELECT 1")
    return {"ok": True, "ts": datetime.now(timezone.utc).isoformat()}
