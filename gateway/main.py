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
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml
from fastapi import Body, Depends, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse

from gateway.auth import require_token
from gateway.metrics import metrics_middleware, render as render_metrics
from paper.contracts import ct_val as _ct_val
from gateway.deps import (
    DB_DSN,
    PROJECT_ROOT,
    STRATEGY_SIGNAL_PREFIX,
    STRATEGY_YAML_MAP,
    close_md_pool,
    close_pool,
    get_md_pool,
    get_pool,
    latest_verdict,
    load_trials,
)

log = logging.getLogger("gateway")

app = FastAPI(title="helivex api-gateway", version="0.8.0")

# Same-origin dashboard talks to the gateway via the Next /gw proxy, so CORS is
# only relevant for direct browser access. Restrict to the dashboard origins
# (override with HELIVEX_CORS_ORIGINS) instead of the spec-invalid "*" + creds.
_cors_origins = [
    o.strip()
    for o in os.environ.get(
        "HELIVEX_CORS_ORIGINS",
        "http://localhost:3400,http://127.0.0.1:3400",
    ).split(",")
    if o.strip()
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=False,
    allow_methods=["GET", "POST", "PUT"],
    allow_headers=["Content-Type", "X-Helivex-Token"],
)


app.middleware("http")(metrics_middleware)


# ─── 补齐 E: edge 式响应脱敏中间件(helixa edge worker 等价,进程内)──────────
# 默认开。redact 敏感键 + DSN/JWT/sk- 值 + 内网主机名。刻意 NOT redact 64-hex
# 指纹(helixa 的 hex≥32 规则会误伤;这里校准更好——指纹是要展示的可复现凭证)。
import re as _re

GW_SANITIZE = os.environ.get("HELIVEX_GW_SANITIZE", "1") not in ("0", "false", "")
_SENS_KEY = _re.compile(
    r"secret|token|api_?key|private|passphrase|password|telegram|chat_id|webhook|db_dsn|_dsn|_host",
    _re.I,
)
_SENS_VAL = _re.compile(
    r"(sk-[A-Za-z0-9]{16,}|eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+"
    r"|postgres(?:ql)?://[^\s\"']+|redis://[^\s\"']+)"
)
_INTERNAL_HOST = _re.compile(
    r"platform-postgres|helios-redis|helios-proxy|quant-rabbitmq|host\.docker\.internal"
)


def _sanitize(obj):
    if isinstance(obj, dict):
        return {
            k: ("[REDACTED]" if _SENS_KEY.search(k) else _sanitize(v))
            for k, v in obj.items()
        }
    if isinstance(obj, list):
        return [_sanitize(x) for x in obj]
    if isinstance(obj, str):
        return _INTERNAL_HOST.sub("[internal]", _SENS_VAL.sub("[REDACTED]", obj))
    return obj


@app.middleware("http")
async def sanitize_response(request: Request, call_next):
    from starlette.responses import Response as _Resp

    response = await call_next(request)
    if not GW_SANITIZE or "application/json" not in response.headers.get(
        "content-type", ""
    ):
        return response
    body = b""
    async for chunk in response.body_iterator:
        body += chunk
    try:
        clean = json.dumps(_sanitize(json.loads(body))).encode()
    except Exception:
        clean = body
    headers = {
        k: v for k, v in response.headers.items() if k.lower() != "content-length"
    }
    return _Resp(
        content=clean,
        status_code=response.status_code,
        headers=headers,
        media_type="application/json",
    )


@app.get("/metrics")
async def metrics() -> PlainTextResponse:
    """Prometheus exposition (request counts + latency, dependency-free)."""
    return PlainTextResponse(render_metrics(), media_type="text/plain; version=0.0.4")


@app.exception_handler(Exception)
async def _unhandled_exception(request: Request, exc: Exception) -> JSONResponse:
    """Catch-all: log the detail server-side, return a generic 500 (no leak).

    HTTPException keeps its own handler, so 400/401/404 still surface normally.
    """
    log.exception("unhandled error on %s %s", request.method, request.url.path)
    return JSONResponse(status_code=500, content={"detail": "internal server error"})


# ─── Lifecycle ────────────────────────────────────────────────────────────────


@app.on_event("startup")
async def _startup() -> None:
    # Non-fatal: if Postgres isn't up yet at boot, don't crash-loop — routes
    # build the pool lazily via get_pool() on first request.
    try:
        await get_pool()
    except Exception as exc:
        log.warning("DB pool not ready at startup: %s — will retry lazily", exc)


@app.on_event("shutdown")
async def _shutdown() -> None:
    await close_pool()
    await close_md_pool()


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
    "trend_dual": "趋势双向 (Donchian 4H)",
    "vwap_mr_dual": "VWAP 均值回归 (1H)",
    "spot_trend": "现货趋势 (日线)",
    "scalp_5m": "⚠ 剥头皮 (VWAP-MR 5M) [NO-GO 观察]",
    "ler_okx": "⚗ 爆仓衰竭回归 (LER, OKX 1m) [研究中]",
}

# Hard-coded gate results for strategies with known permanent verdicts
# scalp_5m: R5 confirmed cost-killed — no gate trial needed, verdict is final
_HARDCODED_GATE: dict[str, dict] = {
    "scalp_5m": {
        "verdict": "no-go",
        "dsr": 0.04,
        "pbo": 0.94,
        "reason": (
            "R5: taker 307%/yr 成本碾死. gross Sharpe +1.33 but net OOS deeply negative. "
            "观察对象 — 测量真实 fill rate/滑点 vs backtest 假设, 非可部署策略."
        ),
    },
    # LER: Phase R 完成(HELIVEX-IMPL_SPEC-LER-001 v1.1),数据源已切 OKX 正向采集,
    # 尚在积累阶段——oskill/omodul 未实现,无信号可判决,不是"待跑 gate"而是"还没到能跑的量"。
    "ler_okx": {
        "verdict": "pending",
        "dsr": None,
        "pbo": None,
        "reason": (
            "研究阶段,非实盘/模拟盘策略。数据正从 OKX 正向采集(见 LER tab),"
            "触发 episode 攒够 Phase 0/CPCV 判决前的样本量预期是月级时间尺度。"
        ),
    },
}


def _latest_gate_metrics(strategy_id: str) -> dict:
    data = load_trials()
    yaml_name = STRATEGY_YAML_MAP.get(strategy_id, Path(strategy_id)).stem
    for entry in reversed(data.get("history", [])):
        cfg_path = entry.get("config", "")
        if strategy_id in cfg_path or yaml_name in cfg_path:
            instruments = entry.get("metrics", {}).get("instruments", {})
            if instruments:
                dsrs = [
                    v.get("dsr")
                    for v in instruments.values()
                    if v.get("dsr") is not None
                ]
                pbos = [
                    v.get("pbo")
                    for v in instruments.values()
                    if v.get("pbo") is not None
                ]
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
        {"name": k, "value": v} for k, v in indic_raw.items() if k != "warmup"
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
    """Return StrategyState-compatible list for all 4 strategies."""
    pool = await get_pool()
    result = []
    for sid, yaml_path in STRATEGY_YAML_MAP.items():
        cfg = _read_yaml(yaml_path) if yaml_path.exists() else {}

        prefix = STRATEGY_SIGNAL_PREFIX.get(sid, sid)
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT COUNT(*) AS n FROM paper.signals WHERE strategy_id LIKE $1",
                prefix,
            )
            frow = await conn.fetchrow(
                "SELECT COUNT(*) AS n FROM paper.fills WHERE strategy_id LIKE $1",
                prefix,
            )
        n_signals = int(row["n"]) if row else 0
        n_fills = int(frow["n"]) if frow else 0

        sl = cfg.get("signal_logic", {})

        # Use hard-coded gate for strategies with permanent verdicts (e.g. scalp_5m)
        if sid in _HARDCODED_GATE:
            gate_obj = _HARDCODED_GATE[sid]
        else:
            verdict = latest_verdict(sid)
            gate_m = _latest_gate_metrics(sid)
            gate_obj = {
                "verdict": ("pass" if verdict == "PASS" else "fail")
                if verdict
                else "pending",
                "dsr": gate_m.get("dsr"),
                "pbo": gate_m.get("pbo"),
                "reason": verdict,
            }

        result.append(
            {
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
                    "direction_mode": str(sl.get("direction_mode", "dual")),
                },
                "gate": gate_obj,
                # Extra context fields (not in StrategyState type, ignored by frontend)
                "n_paper_signals": n_signals,
                "n_fills": n_fills,
                "instruments": cfg.get("instruments", []),
                "timeframe": cfg.get("timeframe", ""),
                "config_path": str(yaml_path.relative_to(PROJECT_ROOT)),
            }
        )
    return result


@app.get("/strategies/{strategy_id}/config")
async def get_strategy_config(strategy_id: str) -> dict:
    """Return full YAML config for a strategy."""
    path = _strategy_id_to_yaml(strategy_id)
    return _read_yaml(path)


@app.put("/strategies/{strategy_id}/config", dependencies=[Depends(require_token)])
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


@app.post("/gate/run", dependencies=[Depends(require_token)])
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

    def _run() -> dict:
        # Heavy CPU-bound gate (numpy / walk-forward). Run in a worker thread with
        # its own event loop so a long gate doesn't block the dashboard's polling
        # on the main event loop.
        return asyncio.run(
            run_gate(config_path, instrument=instrument, verbose=not quiet)
        )

    try:
        result = await asyncio.to_thread(_run)
    except HTTPException:
        raise
    except Exception:
        log.exception("gate run failed for %s", config_path)
        raise HTTPException(500, "gate run failed")
    return result


@app.get("/gate/trials")
async def get_gate_trials() -> dict:
    """Return the full .gate_trials.json history."""
    return load_trials()


# ─── /backtest ────────────────────────────────────────────────────────────────


@app.post("/backtest/run", dependencies=[Depends(require_token)])
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
    from strategy_gate import (
        _fetch_ohlcv,
        _resample_ohlcv,
        _resample_to_1d,
        _signals_to_pnl,
        _walk_forward_gate,
    )  # type: ignore
    import yaml as _yaml
    import importlib

    cfg = _yaml.safe_load(open(config_path))
    strategy_name = cfg["strategy"]

    SMAP = {
        "trend_dual": ("omodul.strategies.trend_dual", "trend_dual"),
        "vwap_mr_dual": ("omodul.strategies.vwap_mr_dual", "vwap_mr_dual"),
        "spot_trend": ("omodul.strategies.spot_trend", "spot_trend"),
    }
    if strategy_name not in SMAP:
        raise HTTPException(400, f"Unknown strategy: {strategy_name}")

    instr = instrument or (cfg.get("instruments", ["BTC-USDT-SWAP"])[0])
    mod_name, fn_name = SMAP[strategy_name]
    mod = importlib.import_module(mod_name)
    strategy_fn = getattr(mod, fn_name)

    try:
        raw = await _fetch_ohlcv(instr, cfg["db_source"])
    except Exception:
        log.exception("backtest DB fetch error for %s", instr)
        raise HTTPException(500, "DB fetch error")

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
    closes = ohlcv["close"]
    cost = out["cost_bps"]
    direction = cfg.get("signal_logic", {}).get("direction", "both")

    pnl = _signals_to_pnl(signals, closes, cost, direction=direction)

    gate_cfg = cfg.get("gate", {})
    n_splits = gate_cfg.get("n_splits", 6)
    embargo = gate_cfg.get("embargo_bars", 50)
    pbo_thr = gate_cfg.get("pbo_threshold", 0.5)
    is_daily = cfg.get("resample_to_1d", False) or cfg.get("timeframe", "") == "1D"
    periods_py = (
        252 if is_daily else (6 * 252 if "1H" in cfg.get("timeframe", "") else 2 * 252)
    )

    gate_result = _walk_forward_gate(pnl, n_splits, embargo, periods_py, pbo_thr)

    # Regime segmentation: split pnl by 200-bar SMA of closes
    closes_arr = list(closes)
    regime_labels: list[str] = []
    for i, c in enumerate(closes_arr):
        window = closes_arr[max(0, i - 200) : i + 1]
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
    limit: int = Query(200, ge=1, le=1000),
) -> dict:
    """Return paper fills with slippage stats."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        where = "WHERE strategy_id=$1" if strategy_id else ""
        args = [strategy_id, limit] if strategy_id else [limit]
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
        fidelity.append(
            {
                "strategy_id": sid,
                "n_signals": n_sigs,
                "n_fills": n_fills,
                "fill_rate": n_fills / n_sigs if n_sigs else None,
                "mean_slippage_bps": float(r["mean_slippage_bps"])
                if r["mean_slippage_bps"]
                else None,
                "p95_slippage_bps": float(r["p95_slippage_bps"])
                if r["p95_slippage_bps"]
                else None,
                "mean_latency_ms": float(r["mean_latency_ms"])
                if r["mean_latency_ms"]
                else None,
            }
        )

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
    """Return cumulative realized paper P&L (FIFO round-trips), segmented by
    strategy/instrument. Same engine as /strategies/{id}/trades and /portfolio."""
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
        for t in sorted(_round_trips(rows), key=lambda t: t["close_time"]):
            cum += t["realized_pnl"]
            pts.append({"ts": t["close_time"], "cum_pnl_usd": round(cum, 4)})
        series[f"{sid}/{inst}"] = pts

    return {"series": series}


# ─── /audit ───────────────────────────────────────────────────────────────────


@app.get("/audit/decisions")
async def get_audit_decisions(
    limit: int = Query(100, ge=1, le=1000),
    strategy_id: str | None = Query(None),
) -> list[dict]:
    """Return recent GOLD-signed signal decisions from paper.signals."""
    pool = await get_pool()
    where = "WHERE strategy_id=$1" if strategy_id else ""
    args = [strategy_id, limit] if strategy_id else [limit]
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

        fp_hex = body["fingerprint_hex"]
        sig_b64 = body["sig_b64"]
        pub_b64 = body.get("public_key_b64") or os.environ.get(
            "HELIVEX_AUDIT_PUBLIC_KEY_B64", ""
        )

        if not pub_b64:
            raise HTTPException(
                400,
                "No public_key_b64 provided and HELIVEX_AUDIT_PUBLIC_KEY_B64 not set",
            )

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
        return {
            "ok": False,
            "reason": "HELIVEX_AUDIT_PUBLIC_KEY_B64 not configured",
            "records": [],
        }

    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    pub_bytes = base64.b64decode(pub_b64)
    pub_key = Ed25519PublicKey.from_public_bytes(pub_bytes)

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


@app.put("/strategies/{strategy_id}/mode", dependencies=[Depends(require_token)])
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
    cfg = _read_yaml(path)
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
        "gate_bypassed": mode in ("paper", "live")
        and latest_verdict(strategy_id) != "PASS"
        and force,
    }


# ─── /paper/account ───────────────────────────────────────────────────────────


@app.get("/paper/account")
async def get_paper_account() -> dict:
    """Return OKX Demo paper account state + today's P&L from fills."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        fills = await conn.fetch(
            """SELECT ts, instrument, side, quantity, signal_price, actual_fill_price
               FROM paper.fills ORDER BY ts ASC"""
        )
    # Realized P&L from FIFO round-trips (canonical) — the actual_fill_price the
    # FIFO uses already embeds slippage, so realized is inherently net. Balance is
    # base + ALL realized (a running account), not just today's slice.
    base = 5000.0
    totals = _realized_totals(fills)

    # Open positions = instruments with a non-zero net signed quantity.
    from collections import defaultdict

    net_qty: dict[str, float] = defaultdict(float)
    for f in fills:
        net_qty[f["instrument"]] += float(f["quantity"]) * (
            1.0 if f["side"] == "BUY" else -1.0
        )
    open_positions = sum(1 for v in net_qty.values() if abs(v) > 1e-9)

    return {
        "balance": round(base + totals["total"], 2),
        "positions": open_positions,
        "pnl_today_gross": round(totals["today"], 2),
        "pnl_today_net": round(totals["today"], 2),
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

    net: dict[str, dict] = defaultdict(
        lambda: {"buy_qty": 0.0, "sell_qty": 0.0, "buy_px": 0.0, "sell_px": 0.0}
    )
    for r in rows:
        inst = r["instrument"]
        if r["side"] == "BUY":
            net[inst]["buy_qty"] += float(r["qty"])
            net[inst]["buy_px"] = float(r["avg_px"])
        else:
            net[inst]["sell_qty"] += float(r["qty"])
            net[inst]["sell_px"] = float(r["avg_px"])

    positions = []
    for inst, d in net.items():
        net_qty = d["buy_qty"] - d["sell_qty"]
        if abs(net_qty) < 1e-9:
            continue
        side = "long" if net_qty > 0 else "short"
        avg_entry = d["buy_px"] if net_qty > 0 else d["sell_px"]
        positions.append(
            {
                "instrument": inst,
                "side": side,
                "quantity": round(abs(net_qty) * _ct_val(inst), 8),  # 张 → 币
                "avg_entry_price": round(avg_entry, 4),
                "current_price": None,
                "unrealized_pnl": 0.0,
                "unrealized_pnl_pct": 0.0,
                "holding_duration": "—",
                "margin_used": None,
                "leverage": None,
                "liquidation_price": None,
            }
        )
    return positions


@app.post(
    "/strategies/{strategy_id}/positions/close",
    dependencies=[Depends(require_token)],
)
async def post_close_position(strategy_id: str, body: dict = Body(...)) -> dict:
    """Queue a manual close for one instrument under this strategy family.

    helixa dashboard has an execution page with per-position manual close;
    helivex previously only had the global kill-switch (blocks new entries,
    never touches existing positions — paper/risk.py's `trip()` is
    deliberately exits-never-blocked). This is the missing per-position
    capability. Cross-process: gateway can't submit Nautilus orders directly
    (that requires a live Strategy instance's order_factory, which only
    exists inside the paper container), so this queues a row in
    paper.manual_close_requests; the target strategy instance polls and
    claims its own requests (paper/strategies/_guard.py::start_manual_close_poll)
    and executes the same OKX-safe close path used for shutdown/reconnect.
    """
    instrument = body.get("instrument")
    if not instrument:
        raise HTTPException(400, "instrument required")
    prefix = _prefix_for(strategy_id)  # "{base}_%"
    base = prefix.removesuffix("_%")
    inst_key = instrument.replace(".", "_").replace("-", "_").lower()
    exact_strategy_id = f"{base}_{inst_key}"

    from paper.db import request_manual_close

    pool = await get_pool()
    async with pool.acquire() as conn:
        req_id = await request_manual_close(
            conn, exact_strategy_id, instrument, requested_by="dashboard"
        )
    return {
        "ok": True,
        "request_id": req_id,
        "strategy_id": exact_strategy_id,
        "instrument": instrument,
        "status": "pending",
    }


# ─── /strategies/{id}/trades ──────────────────────────────────────────────────


def _fmt_duration(delta) -> str:
    secs = int(max(0, delta.total_seconds()))
    h, rem = divmod(secs, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m}m"
    if m:
        return f"{m}m {s}s"
    return f"{s}s"


# OKX USDT-perp taker 费率。paper.fills 实测 fill_type 100% taker(全部策略只发
# IOC 市价单),故两条腿均按 taker 估算;maker 费率留作将来接 post-only 后使用。
TAKER_FEE_RATE = 0.0005
MAKER_FEE_RATE = 0.0002


def _round_trips(rows: list) -> list[dict]:
    """FIFO round-trip extraction from fills (ts-ASC). Each reducing fill closes
    open lots oldest-first and emits a realized trade. Total realized P&L matches
    the avg-cost figure in paper.risk; FIFO just gives clean per-trade entry ts/px.
    Quantities in fills are OKX contracts (张); P&L / notional / quantity are scaled
    by ctVal here so every downstream surface reports real coin/USD terms."""
    from collections import defaultdict, deque

    lots: dict[str, deque] = defaultdict(
        deque
    )  # instrument -> deque[[qty_signed, px, ts]]
    trades: list[dict] = []
    seq = 0
    for r in rows:
        inst = r["instrument"]
        ctv = _ct_val(inst)  # 张 → 币/美元 换算(BTC 0.01 / ETH 0.1 / SOL 1)
        px = float(r["actual_fill_price"])
        ts = r["ts"]
        q = float(r["quantity"]) * (1.0 if r["side"] == "BUY" else -1.0)
        dq = lots[inst]
        # close against opposite-signed lots first (FIFO)
        while abs(q) > 1e-12 and dq and (dq[0][0] > 0) != (q > 0):
            lot = dq[0]
            lot_sign = 1.0 if lot[0] > 0 else -1.0
            q_sign = 1.0 if q > 0 else -1.0
            closed = min(abs(lot[0]), abs(q))
            gross = lot_sign * (px - lot[1]) * closed * ctv
            entry_notional = lot[1] * closed * ctv
            exit_notional = px * closed * ctv
            # 手续费此前硬编码 0,而 paper.fills 实测 100% taker(1888/1888 笔),
            # 于是所有 P&L 口径显示的都是毛值。venue 不回报手续费,故按 OKX taker
            # 费率估算两条腿。估算值单列 fees,并从 realized_pnl 扣除 —— 后者被
            # 三条净值曲线 + stats + 归因共用,单点扣费才能让全站口径一致。
            fees = (entry_notional + exit_notional) * TAKER_FEE_RATE
            pnl = gross - fees
            seq += 1
            trades.append(
                {
                    "trade_id": f"{inst}-{seq}",
                    "open_time": lot[2].isoformat(),
                    "close_time": ts.isoformat(),
                    "instrument": inst,
                    "side": "long" if lot_sign > 0 else "short",
                    "entry_price": round(lot[1], 6),
                    "exit_price": round(px, 6),
                    "quantity": round(closed * ctv, 8),
                    "realized_pnl": round(pnl, 6),
                    "realized_pnl_pct": round(pnl / entry_notional * 100, 4)
                    if entry_notional
                    else 0.0,
                    "gross_pnl": round(gross, 6),
                    "fees": round(fees, 6),
                    "holding_duration": _fmt_duration(ts - lot[2]),
                    "trigger_signal": "—",
                    "exit_reason": "close",
                }
            )
            lot[0] -= lot_sign * closed
            q -= q_sign * closed
            if abs(lot[0]) < 1e-12:
                dq.popleft()
        # remainder opens a new lot
        if abs(q) > 1e-12:
            dq.append([q, px, ts])
    return trades


# ── Canonical P&L: every surface below derives from _round_trips (FIFO realized)
#    so equity, /pnl, /paper/account and /portfolio/* can never disagree. Before
#    this, equity used signal-vs-fill, /pnl used a slippage-cost proxy, and
#    /paper/account used a third formula — three different numbers for one book.


def _equity_points(rows: list, base: float) -> list[dict]:
    """Realized-P&L equity curve from FIFO round-trips: steps at each trade close
    by that trade's realized P&L, carrying a running (fractional) drawdown."""
    trades = sorted(_round_trips(rows), key=lambda t: t["close_time"])
    cum = peak = 0.0
    pts: list[dict] = []
    for t in trades:
        cum += t["realized_pnl"]
        peak = max(peak, cum)
        denom = base + peak
        pts.append(
            {
                "date": t["close_time"],
                "equity": round(base + cum, 4),
                "drawdown": round(-(peak - cum) / denom, 6) if denom else 0.0,
                "realized_pnl": round(cum, 4),
            }
        )
    return pts


def _realized_totals(rows: list) -> dict:
    """All-time and today's realized P&L (+ trade count) from FIFO round-trips."""
    trades = _round_trips(rows)
    total = sum(t["realized_pnl"] for t in trades)
    today = datetime.now(timezone.utc).date().isoformat()
    today_pnl = sum(t["realized_pnl"] for t in trades if t["close_time"][:10] == today)
    return {"total": total, "today": today_pnl, "n_trades": len(trades)}


@app.get("/strategies/{strategy_id}/trades")
async def get_strategy_trades(
    strategy_id: str,
    limit: int = Query(200, ge=1, le=1000),
) -> list:
    """Return Trade[] — realized FIFO round-trips from paper.fills, newest first."""
    prefix = _prefix_for(strategy_id)
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT ts, instrument, side, quantity, signal_price, actual_fill_price
               FROM paper.fills WHERE strategy_id LIKE $1 ORDER BY ts ASC""",
            prefix,
        )
    trades = _round_trips(rows)
    trades.reverse()  # newest first
    return trades[:limit]


# ─── /strategies/{id}/equity ──────────────────────────────────────────────────


@app.get("/strategies/{strategy_id}/equity")
async def get_strategy_equity(strategy_id: str) -> dict:
    """Return StrategyEquity with equity curve points (5000 base + cum P&L)."""
    prefix = _prefix_for(strategy_id)
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT ts, instrument, side, quantity, signal_price, actual_fill_price
               FROM paper.fills WHERE strategy_id LIKE $1 ORDER BY ts ASC""",
            prefix,
        )
    base = 5000.0
    points = _equity_points(rows, base)
    if not points:
        points = [
            {
                "date": datetime.now(timezone.utc).isoformat(),
                "equity": base,
                "drawdown": 0.0,
                "realized_pnl": 0.0,
            }
        ]
    return {"points": points, "by_instrument": None}


# ─── /strategies/{id}/signals ─────────────────────────────────────────────────


@app.get("/strategies/{strategy_id}/signals")
async def get_strategy_signals(
    strategy_id: str,
    limit: int = Query(100, ge=1, le=1000),
) -> list:
    """Return SignalLog[] with indicator snapshots for a strategy."""
    prefix = _prefix_for(strategy_id)
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT ts, instrument, action, signal_price, sig_b64, indicators
               FROM paper.signals WHERE strategy_id LIKE $1
               ORDER BY ts DESC LIMIT $2""",
            prefix,
            limit,
        )
    return [_row_to_signal_log(r) for r in rows]


# ─── /strategies/{id}/stats ───────────────────────────────────────────────────


@app.get("/strategies/{strategy_id}/stats")
async def get_strategy_stats(
    strategy_id: str,
    since: str | None = Query(
        None, description="ISO 时间,只统计此后的回合(出场结构变更后分段统计用)"
    ),
) -> dict:
    """Return StrategyStats computed from realized FIFO round-trips of paper.fills."""
    import statistics

    since_dt = datetime.fromisoformat(since.replace("Z", "+00:00")) if since else None
    prefix = _prefix_for(strategy_id)
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT ts, instrument, side, quantity, signal_price, actual_fill_price
               FROM paper.fills WHERE strategy_id LIKE $1
                 AND ($2::timestamptz IS NULL OR ts >= $2::timestamptz)
               ORDER BY ts ASC""",
            prefix,
            since_dt,
        )
    trades = _round_trips(rows)
    n = len(trades)
    pnls = [t["realized_pnl"] for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    total_pnl = sum(pnls)
    gross_win = sum(wins)
    gross_loss = -sum(losses)

    # max drawdown over the cumulative realized-P&L curve, as a FRACTION of base
    # equity (0..1), matching the win_rate convention. The frontend multiplies by
    # 100 for display; returning a percent here double-scaled it (0.5% → "54%").
    cum = peak = maxdd = 0.0
    for p in pnls:
        cum += p
        peak = max(peak, cum)
        maxdd = max(maxdd, peak - cum)
    base_equity = 5000.0

    # per-trade Sharpe proxy (mean / population std of trade P&Ls)
    sharpe = 0.0
    if len(pnls) > 1:
        sd = statistics.pstdev(pnls)
        if sd > 0:
            sharpe = statistics.mean(pnls) / sd

    avg_hold = "—"
    if trades:
        durs = [
            (
                datetime.fromisoformat(t["close_time"])
                - datetime.fromisoformat(t["open_time"])
            ).total_seconds()
            for t in trades
        ]
        from datetime import timedelta

        avg_hold = _fmt_duration(timedelta(seconds=sum(durs) / len(durs)))

    return {
        "total_trades": n,
        "win_rate": round(len(wins) / n, 4) if n else 0.0,
        "profit_factor": round(gross_win / gross_loss, 4)
        if gross_loss > 0
        else (0.0 if gross_win == 0 else None),
        "avg_holding": avg_hold,
        "max_drawdown": round(maxdd / base_equity, 4),
        "forward_sharpe": round(sharpe, 4),
        "total_pnl": round(total_pnl, 4),
        "backtest_oos_sharpe": None,
        "sample_sufficient": n >= 30,
    }


# ─── /strategies/{id}/execution ───────────────────────────────────────────────


@app.get("/strategies/{strategy_id}/execution")
async def get_strategy_execution(
    strategy_id: str,
    limit: int = Query(200, ge=1, le=1000),
) -> dict:
    """Return StrategyExecution with fill list and slippage summary."""
    prefix = _prefix_for(strategy_id)
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT id, ts, instrument, signal_price, actual_fill_price, fill_type, slippage_bps
               FROM paper.fills WHERE strategy_id LIKE $1
               ORDER BY ts DESC LIMIT $2""",
            prefix,
            limit,
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
        "avg_slippage_bps": round(float(agg["mean_slip"]), 4)
        if agg and agg["mean_slip"]
        else 0.0,
        "max_slippage_bps": round(float(agg["max_slip"]), 4)
        if agg and agg["max_slip"]
        else 0.0,
        "backtest_assumed_bps": 2,
    }


# ─── /portfolio/equity ────────────────────────────────────────────────────────


@app.get("/portfolio/equity")
async def get_portfolio_equity() -> dict:
    """Return PortfolioEquity: combined curve + per-strategy breakdown."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT ts, strategy_id, instrument, side, quantity, signal_price, actual_fill_price
               FROM paper.fills ORDER BY ts ASC"""
        )

    from collections import defaultdict

    by_strat_rows: dict[str, list] = defaultdict(list)
    for r in rows:
        by_strat_rows[r["strategy_id"]].append(r)

    BASE_PER_STRATEGY = 5000.0
    by_strategy = []
    # Combined curve: union of ALL strategies' FIFO trades on one timeline (never
    # cross-matched between strategies — each strategy's lots are FIFO'd alone).
    all_trades: list[dict] = []

    for sid, fills in by_strat_rows.items():
        trades = _round_trips(fills)
        all_trades.extend(trades)
        by_strategy.append(
            {
                "strategy_id": sid,
                "points": _equity_points(fills, BASE_PER_STRATEGY),
                "contribution_pct": 0.0,
            }
        )

    n = max(1, len(by_strat_rows))
    combined_base = BASE_PER_STRATEGY * n
    cum = peak = 0.0
    combined = []
    for t in sorted(all_trades, key=lambda t: t["close_time"]):
        cum += t["realized_pnl"]
        peak = max(peak, cum)
        denom = combined_base + peak
        combined.append(
            {
                "date": t["close_time"],
                "equity": round(combined_base + cum, 2),
                "drawdown": round(-(peak - cum) / denom, 6) if denom else 0.0,
            }
        )
    if not combined:
        now = datetime.now(timezone.utc).isoformat()
        combined = [{"date": now, "equity": combined_base, "drawdown": 0.0}]

    return {"combined": combined, "by_strategy": by_strategy}


@app.get("/portfolio/attribution")
async def get_portfolio_attribution() -> dict:
    """Per-STRATEGY realized-P&L attribution (helixa Grafana signal-attribution 的等价物,
    诚实修正:helixa 归因到 engine 因为 engine 直接驱动实盘;helivex 是 4 个策略在成交、
    共识仍 observe,所以归因到策略才有意义。引擎级归因要等 enforce 后引擎驱动执行才成立)。
    每策略:realized P&L、占毛额比例、成交数、胜率、单笔均益、最佳/最差。"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT ts, strategy_id, instrument, side, quantity, actual_fill_price
               FROM paper.fills ORDER BY ts ASC"""
        )
    from collections import defaultdict

    by_strat: dict[str, list] = defaultdict(list)
    for r in rows:
        by_strat[r["strategy_id"]].append(r)

    strat_stats: list[dict] = []
    for sid, fills in by_strat.items():
        trades = _round_trips(fills)
        pnls = [t["realized_pnl"] for t in trades]
        n = len(pnls)
        realized = sum(pnls)
        wins = sum(1 for p in pnls if p > 0)
        strat_stats.append(
            {
                "strategy_id": sid,
                "realized_pnl": round(realized, 2),
                "n_trades": n,
                "win_rate": round(wins / n, 4) if n else None,
                "avg_pnl": round(realized / n, 4) if n else None,
                "best": round(max(pnls), 2) if pnls else None,
                "worst": round(min(pnls), 2) if pnls else None,
            }
        )

    gross = sum(abs(s["realized_pnl"]) for s in strat_stats) or 1.0
    for s in strat_stats:
        s["pct_of_gross"] = round(abs(s["realized_pnl"]) / gross, 4)
    strat_stats.sort(key=lambda s: s["realized_pnl"], reverse=True)
    return {
        "as_of": datetime.now(timezone.utc).isoformat(),
        "total_realized": round(sum(s["realized_pnl"] for s in strat_stats), 2),
        "by_strategy": strat_stats,
    }


# ─── 补齐 I: FGI 情绪 + 统一事件时间线 ────────────────────────────────────────


@app.get("/sentiment/fgi")
async def get_sentiment_fgi() -> dict:
    """Fear & Greed index + contrarian bias (helixa KpiStrip/Regime 的 FGI「反转」等价物).
    读 iris md.sentiment(Alternative.me)。极度恐惧→逆向看多(bias>0),极度贪婪→看空。"""
    md = await get_md_pool()
    async with md.acquire() as conn:
        reg = await conn.fetchval("SELECT to_regclass('md.sentiment')")
        if not reg:
            return {
                "value": None,
                "classification": None,
                "contrarian_bias": 0.0,
                "ts": None,
            }
        row = await conn.fetchrow(
            """SELECT value, classification, ts FROM md.sentiment
               WHERE metric='fear_greed_index' ORDER BY ts DESC LIMIT 1"""
        )
    if not row:
        return {
            "value": None,
            "classification": None,
            "contrarian_bias": 0.0,
            "ts": None,
        }
    v = float(row["value"])
    bias = max(-1.0, min(1.0, (50.0 - v) / 50.0))  # contrarian: low FGI → bullish
    stance = "看多(逆向)" if bias > 0.15 else "看空(逆向)" if bias < -0.15 else "中性"
    return {
        "value": v,
        "classification": row["classification"],
        "contrarian_bias": round(bias, 3),
        "contrarian_stance": stance,
        "ts": row["ts"].isoformat() if row["ts"] else None,
    }


@app.get("/events/timeline")
async def get_events_timeline(limit: int = Query(40, ge=1, le=200)) -> dict:
    """Unified event stream (helixa StrategyStream 等价物):成交 + 风控事件 + 共识轮次,
    按时间倒序合并成一条时间线。"""
    pool = await get_pool()
    out: list[dict] = []
    async with pool.acquire() as conn:
        fills = await conn.fetch(
            """SELECT ts, strategy_id, instrument, side, actual_fill_price
               FROM paper.fills ORDER BY ts DESC LIMIT $1""",
            limit,
        )
        for f in fills:
            out.append(
                {
                    "ts": f["ts"].isoformat(),
                    "category": "fill",
                    "label": f"{f['side']} {str(f['instrument']).split('.')[0].split('-')[0]} @ {float(f['actual_fill_price'])}",
                    "detail": f["strategy_id"],
                }
            )
        if await conn.fetchval("SELECT to_regclass('paper.risk_events')"):
            revs = await conn.fetch(
                "SELECT ts, kind, entity_id, severity, message FROM paper.risk_events ORDER BY id DESC LIMIT $1",
                limit,
            )
            for r in revs:
                out.append(
                    {
                        "ts": r["ts"].isoformat(),
                        "category": f"risk:{r['kind']}",
                        "label": r["message"] or r["kind"],
                        "detail": f"{r['entity_id'] or ''} ({r['severity'] or ''})",
                    }
                )
        if await conn.fetchval("SELECT to_regclass('paper.consensus_signals')"):
            cons = await conn.fetch(
                """SELECT cycle_ts, instrument, final_direction, should_execute
                   FROM paper.consensus_signals ORDER BY cycle_ts DESC LIMIT $1""",
                limit,
            )
            for c in cons:
                out.append(
                    {
                        "ts": c["cycle_ts"].isoformat(),
                        "category": "consensus",
                        "label": f"{str(c['instrument']).split('-')[0]} {c['final_direction']}"
                        + (" ✅可执行" if c["should_execute"] else " (观察)"),
                        "detail": "共识轮次",
                    }
                )
    out.sort(key=lambda e: e["ts"], reverse=True)
    return {"events": out[:limit]}


# ─── /portfolio/correlation ───────────────────────────────────────────────────


@app.get("/portfolio/correlation")
async def get_portfolio_correlation() -> dict:
    """Return CorrelationMatrix {strategies, matrix: number[][]} from daily P&L."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT DATE(ts) AS day, strategy_id, instrument,
                      SUM(CASE WHEN side='SELL' THEN 1.0 ELSE -1.0 END
                          * quantity * (actual_fill_price - COALESCE(signal_price, actual_fill_price))
                      ) AS daily_pnl
               FROM paper.fills GROUP BY DATE(ts), strategy_id, instrument ORDER BY day""",
        )

    from collections import defaultdict

    # 按 instrument 分组后乘 ctVal(张→币),再按 (day, strategy) 汇总——否则一个策略跨
    # BTC/ETH/SOL 的日 P&L 权重被合约面值扭曲,相关性矩阵失真。
    daily: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    strats: set[str] = set()
    for r in rows:
        daily[str(r["day"])][r["strategy_id"]] += float(r["daily_pnl"]) * _ct_val(
            r["instrument"]
        )
        strats.add(r["strategy_id"])

    strat_list = sorted(strats)
    if len(strat_list) < 2:
        return {
            "strategies": strat_list,
            "matrix": [[1.0]] if len(strat_list) == 1 else [],
        }

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
        return (
            round(num / (da * db), 4)
            if da > 1e-12 and db > 1e-12
            else (1.0 if a == b else 0.0)
        )

    matrix = [[_corr(vecs[s1], vecs[s2]) for s2 in strat_list] for s1 in strat_list]
    return {"strategies": strat_list, "matrix": matrix}


# ─── /portfolio/summary ───────────────────────────────────────────────────────


@app.get("/portfolio/summary")
async def get_portfolio_summary() -> dict:
    """Return PortfolioSummary with realized P&L and exposure."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        fill_rows = await conn.fetch(
            """SELECT ts, instrument, side, quantity, signal_price, actual_fill_price
               FROM paper.fills ORDER BY ts ASC"""
        )

    from collections import defaultdict

    net_exp: dict[str, float] = defaultdict(float)
    for r in fill_rows:
        sign = 1.0 if r["side"] == "BUY" else -1.0
        net_exp[r["instrument"]] += (
            sign * float(r["quantity"]) * _ct_val(r["instrument"])
        )
    # Canonical realized P&L (FIFO), consistent with every other P&L surface.
    total_pnl = _realized_totals(fill_rows)["total"]

    net_exposure = [
        {"instrument": k, "net": round(v, 8)}
        for k, v in net_exp.items()
        if abs(v) > 1e-9
    ]
    return {
        "total_positions": len(net_exposure),
        "total_unrealized_pnl": 0.0,
        "total_realized_pnl": round(total_pnl, 4),
        "net_exposure": net_exposure,
        "margin_used": 0.0,
        "available": 15000.0,
    }


# ─── /portfolio/cvar_weights, /portfolio/position_caps (3O CVaR risk phase 1) ──
# Stage A: observe-only. See ops/scripts/cvar_risk_adapter.py + paper/risk.py's
# DYNAMIC_RISK_ENFORCE / gate_entry_dynamic for the enforcement rollout.


@app.get("/portfolio/cvar_weights")
async def get_portfolio_cvar_weights() -> dict:
    """Latest CVaR-Sharpe portfolio weights cycle (paper.portfolio_weights)."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT instrument, weight, method, fallback_reason,
                      portfolio_cvar_95, lookback_days, n_obs, cycle_ts
               FROM paper.portfolio_weights
               WHERE cycle_ts = (SELECT MAX(cycle_ts) FROM paper.portfolio_weights)
               ORDER BY instrument"""
        )
    if not rows:
        return {
            "as_of": None,
            "method": None,
            "fallback_reason": None,
            "portfolio_cvar_95": None,
            "lookback_days": None,
            "n_obs": None,
            "weights": [],
        }
    return {
        "as_of": rows[0]["cycle_ts"].isoformat(),
        "method": rows[0]["method"],
        "fallback_reason": rows[0]["fallback_reason"],
        "portfolio_cvar_95": float(rows[0]["portfolio_cvar_95"])
        if rows[0]["portfolio_cvar_95"] is not None
        else None,
        "lookback_days": rows[0]["lookback_days"],
        "n_obs": rows[0]["n_obs"],
        "weights": [
            {"instrument": r["instrument"], "weight": float(r["weight"])} for r in rows
        ],
    }


@app.get("/portfolio/position_caps")
async def get_portfolio_position_caps() -> dict:
    """Latest 3-tier position-cap cycle (paper.position_caps) + current enforce mode."""
    from paper.risk import DYNAMIC_RISK_ENFORCE

    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT instrument, tier1_headroom, tier2_atr_cap, tier3_corr_clip,
                      effective_cap_usd, binding_tier, reasons, cycle_ts
               FROM paper.position_caps
               WHERE cycle_ts = (SELECT MAX(cycle_ts) FROM paper.position_caps)
               ORDER BY instrument"""
        )
    return {
        "as_of": rows[0]["cycle_ts"].isoformat() if rows else None,
        "enforce_mode": DYNAMIC_RISK_ENFORCE,
        "caps": [
            {
                "instrument": r["instrument"],
                "tier1_headroom": float(r["tier1_headroom"]),
                "tier2_atr_cap": float(r["tier2_atr_cap"]),
                "tier3_corr_clip": float(r["tier3_corr_clip"]),
                "effective_cap_usd": float(r["effective_cap_usd"]),
                "binding_tier": r["binding_tier"],
                "reasons": json.loads(r["reasons"]) if r["reasons"] else [],
            }
            for r in rows
        ],
    }


# ─── /consensus/risk_eval (3O Phase 6, consensus→risk pipeline, observe) ──────


@app.get("/consensus/risk_eval")
async def get_consensus_risk_eval() -> dict:
    """Latest consensus→risk pipeline evaluation (paper.consensus_risk_eval).
    Shows, per instrument, whether a consensus signal WOULD pass the full risk
    pipeline (crisis override + 3-tier clip + fee/edge) and at what size —
    observe-only, no orders placed. `enforce_mode` is HELIVEX_CONSENSUS_ENFORCE."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        exists = await conn.fetchval("SELECT to_regclass('paper.consensus_risk_eval')")
        if not exists:
            return {"as_of": None, "enforce_mode": "observe", "evals": []}
        rows = await conn.fetch(
            """SELECT instrument, direction, should_execute, approved, final_notional,
                      blocking_stage, crisis_scaled, enforce_mode, reasons, cycle_ts
               FROM paper.consensus_risk_eval
               WHERE cycle_ts = (SELECT MAX(cycle_ts) FROM paper.consensus_risk_eval)
               ORDER BY instrument"""
        )
    return {
        "as_of": rows[0]["cycle_ts"].isoformat() if rows else None,
        "enforce_mode": rows[0]["enforce_mode"] if rows else "observe",
        "evals": [
            {
                "instrument": r["instrument"],
                "direction": r["direction"],
                "should_execute": r["should_execute"],
                "approved": r["approved"],
                "final_notional": float(r["final_notional"])
                if r["final_notional"] is not None
                else None,
                "blocking_stage": r["blocking_stage"],
                "crisis_scaled": r["crisis_scaled"],
                "reasons": json.loads(r["reasons"]) if r["reasons"] else [],
            }
            for r in rows
        ],
    }


# ─── 补齐 B: helixa internal-endpoint parity(decision-trail 等)──────────────


@app.get("/decision-trail/recent")
async def get_decision_trail_recent(limit: int = Query(20, ge=1, le=100)) -> dict:
    """Recent 3O decision trails across consensus / regime / portfolio-weights.
    STRONGER than helixa's decision-trail: every entry is fingerprinted (64-hex,
    reproducible) with step-by-step layer/callable provenance, not just a
    free-text reasoning string."""
    pool = await get_pool()
    out: list[dict] = []
    async with pool.acquire() as conn:
        for tbl, kind in [
            ("paper.consensus_signals", "consensus"),
            ("paper.regime_state", "regime"),
            ("paper.portfolio_weights", "portfolio_weight"),
        ]:
            reg = await conn.fetchval("SELECT to_regclass($1)", tbl)
            if not reg:
                continue
            has_trail = await conn.fetchval(
                "SELECT 1 FROM information_schema.columns WHERE table_schema='paper' "
                "AND table_name=$1 AND column_name='decision_trail'",
                tbl.split(".")[1],
            )
            trail_col = "decision_trail" if has_trail else "detail"
            rows = await conn.fetch(
                f"""SELECT instrument, fingerprint, {trail_col} AS trail, cycle_ts
                    FROM {tbl} ORDER BY cycle_ts DESC LIMIT $1""",
                limit,
            )
            for r in rows:
                trail = json.loads(r["trail"]) if r["trail"] else {}
                out.append(
                    {
                        "kind": kind,
                        "instrument": r["instrument"],
                        "fingerprint": r["fingerprint"],
                        "ts": r["cycle_ts"].isoformat(),
                        "steps": trail.get("steps")
                        if isinstance(trail, dict)
                        else None,
                        "trail": trail,
                    }
                )
    out.sort(key=lambda x: x["ts"], reverse=True)
    return {"decision_trail": out[:limit]}


@app.get("/cross-exposure")
async def get_cross_exposure() -> dict:
    """Cross-strategy net exposure per instrument (helixa cross_strategy_net
    equivalent), from paper.fills FIFO round-trips + open lots."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT instrument, strategy_id,
                      SUM(CASE WHEN side='BUY' THEN quantity ELSE -quantity END) AS net_qty
               FROM paper.fills GROUP BY instrument, strategy_id"""
        )
    from collections import defaultdict

    by_inst: dict[str, dict] = defaultdict(lambda: {"net": 0.0, "by_strategy": {}})
    for r in rows:
        q = float(r["net_qty"] or 0.0) * _ct_val(r["instrument"])  # 张 → 币
        if abs(q) < 1e-9:
            continue
        by_inst[r["instrument"]]["net"] += q
        by_inst[r["instrument"]]["by_strategy"][r["strategy_id"]] = round(q, 8)
    return {
        "cross_exposure": [
            {
                "instrument": k,
                "net_qty": round(v["net"], 8),
                "by_strategy": v["by_strategy"],
            }
            for k, v in by_inst.items()
        ]
    }


@app.get("/derivatives/{symbol}")
async def get_derivatives(symbol: str) -> dict:
    """Latest funding + OI for a symbol (helixa derivatives/{symbol} equivalent)
    from the iris/md schema (funding_rates / oi)."""
    md = await get_md_pool()
    swap = symbol if symbol.endswith("-SWAP") else f"{symbol}-SWAP"
    async with md.acquire() as conn:
        funding = await conn.fetchrow(
            """SELECT funding_rate, realized_rate, funding_time FROM md.funding_settled
               WHERE venue='okx' AND inst_id=$1 ORDER BY funding_time DESC LIMIT 1""",
            swap,
        )
        oi = await conn.fetchrow(
            """SELECT oi_contracts, oi_coin, oi_usd, ts FROM md.oi
               WHERE venue='okx' AND inst_id=$1 ORDER BY ts DESC LIMIT 1""",
            swap,
        )
    return {
        "symbol": swap,
        "funding": {
            "rate": float(funding["funding_rate"]) if funding else None,
            "realized_rate": float(funding["realized_rate"])
            if funding and funding["realized_rate"] is not None
            else None,
            "ts": funding["funding_time"].isoformat() if funding else None,
        },
        "open_interest": {
            "contracts": float(oi["oi_contracts"])
            if oi and oi["oi_contracts"] is not None
            else None,
            "coin": float(oi["oi_coin"]) if oi and oi["oi_coin"] is not None else None,
            "usd": float(oi["oi_usd"]) if oi and oi["oi_usd"] is not None else None,
            "ts": oi["ts"].isoformat() if oi else None,
        },
    }


@app.get("/multitime-trend/{symbol}")
async def get_multitime_trend(symbol: str) -> dict:
    """Multi-timeframe TA trend for a symbol (helixa multitime-trend equivalent)
    from the latest ta_multi engine signal's per-timeframe detail."""
    pool = await get_pool()
    inst = symbol if symbol.endswith("-SWAP") else f"{symbol}-SWAP"
    async with pool.acquire() as conn:
        exists = await conn.fetchval("SELECT to_regclass('paper.engine_signals')")
        if not exists:
            return {"symbol": inst, "per_timeframe": {}, "as_of": None}
        row = await conn.fetchrow(
            """SELECT detail, score, direction, cycle_ts FROM paper.engine_signals
               WHERE engine='ta_multi' AND instrument=$1
               ORDER BY cycle_ts DESC LIMIT 1""",
            inst,
        )
    if not row:
        return {"symbol": inst, "per_timeframe": {}, "as_of": None}
    detail = json.loads(row["detail"]) if row["detail"] else {}
    return {
        "symbol": inst,
        "combined_direction": row["direction"],
        "combined_score": float(row["score"]) if row["score"] is not None else None,
        "per_timeframe": detail.get("per_tf", {}),
        "as_of": row["cycle_ts"].isoformat(),
    }


@app.get("/ohlcv/{symbol}")
async def get_ohlcv(symbol: str, limit: int = Query(120, ge=10, le=500)) -> dict:
    """Recent 5m candles + fill markers for a symbol (helixa internal/ohlcv +
    PriceChart trade-marker equivalent). Reads helivex market_data.ohlcv_5m."""
    inst = symbol if symbol.endswith("-SWAP") else f"{symbol}-SWAP"
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT bar_close_ts, open, high, low, close, volume
               FROM market_data.ohlcv_5m
               WHERE source='okx_swap_5m' AND instrument=$1
               ORDER BY bar_close_ts DESC LIMIT $2""",
            inst,
            limit,
        )
        rows = list(reversed(rows))
        first_ts = rows[0]["bar_close_ts"] if rows else None
        fills = []
        if first_ts is not None:
            fills = await conn.fetch(
                """SELECT ts, side, actual_fill_price, strategy_id
                   FROM paper.fills WHERE split_part(instrument,'.',1)=$1 AND ts >= $2
                   ORDER BY ts ASC LIMIT 200""",
                inst,
                first_ts,
            )
    from collections import Counter

    _ts_counts = Counter(m["ts"] for m in fills)
    return {
        "instrument": inst,
        "candles": [
            {
                "ts": r["bar_close_ts"].isoformat(),
                "o": float(r["open"]),
                "h": float(r["high"]),
                "l": float(r["low"]),
                "c": float(r["close"]),
            }
            for r in rows
        ],
        "markers": [
            {
                "ts": m["ts"].isoformat(),
                "side": m["side"].lower(),
                "price": float(m["actual_fill_price"]),
                "strategy": m["strategy_id"],
                # burst = identical-ts duplicate fills (the replay-burst signature the
                # eval_burst_recurrence watcher flags); shown as ⚠ on the chart.
                "burst": _ts_counts[m["ts"]] > 1,
            }
            for m in fills
        ],
    }


# ─── /regime (3O Phase 2, advisory market-regime classification) ──────────────


@app.get("/regime")
async def get_regime() -> dict:
    """Latest per-instrument market regime (crisis/trend/range) from
    paper.regime_state. ADVISORY only — helivex research (646dc71) found HMM
    regimes have no OOS persistence, so consumers treat this as one soft input,
    never a hard gate."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        exists = await conn.fetchval("SELECT to_regclass('paper.regime_state')")
        if not exists:
            return {"as_of": None, "advisory": True, "regimes": []}
        rows = await conn.fetch(
            """SELECT instrument, state, confidence, method_used, rows_used,
                      advisory, detail, cycle_ts
               FROM paper.regime_state
               WHERE cycle_ts = (SELECT MAX(cycle_ts) FROM paper.regime_state)
               ORDER BY instrument"""
        )
    return {
        "as_of": rows[0]["cycle_ts"].isoformat() if rows else None,
        "advisory": True,
        "regimes": [
            {
                "instrument": r["instrument"],
                "state": r["state"],
                "confidence": float(r["confidence"])
                if r["confidence"] is not None
                else None,
                "method_used": r["method_used"],
                "rows_used": r["rows_used"],
                "detail": json.loads(r["detail"]) if r["detail"] else {},
            }
            for r in rows
        ],
    }


# ─── /engines (3O Phase 4, per-engine directional signals) ────────────────────


@app.get("/engines")
async def get_engines() -> dict:
    """Latest per-engine directional signals (paper.engine_signals). `promoted`
    = passed the engine's own gate (ML: DSR; TA: rule-based always true; LLM:
    disabled slot). Un-promoted engines are observe-only — unlike helixa, whose
    DSR gate never fired so a 0.2315-accuracy model kept voting."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        exists = await conn.fetchval("SELECT to_regclass('paper.engine_signals')")
        if not exists:
            return {"as_of": None, "engines": []}
        rows = await conn.fetch(
            """SELECT engine, instrument, direction, score, confidence, promoted,
                      detail, cycle_ts
               FROM paper.engine_signals
               WHERE cycle_ts = (SELECT MAX(cycle_ts) FROM paper.engine_signals)
               ORDER BY engine, instrument"""
        )
    return {
        "as_of": rows[0]["cycle_ts"].isoformat() if rows else None,
        "engines": [
            {
                "engine": r["engine"],
                "instrument": r["instrument"],
                "direction": r["direction"],
                "score": float(r["score"]) if r["score"] is not None else None,
                "confidence": float(r["confidence"])
                if r["confidence"] is not None
                else None,
                "promoted": r["promoted"],
                "detail": json.loads(r["detail"]) if r["detail"] else {},
            }
            for r in rows
        ],
    }


@app.get("/engines/weights")
async def get_engine_weights() -> dict:
    """Per-engine EWMA-learned weights (paper.engine_weights): base vs dynamic +
    rolling accuracy. Feeds the consensus; exposed for the TG control bot."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        exists = await conn.fetchval("SELECT to_regclass('paper.engine_weights')")
        if not exists:
            return {"weights": []}
        rows = await conn.fetch(
            """SELECT engine, base_weight, accuracy, dyn_weight, updated_at
               FROM paper.engine_weights ORDER BY dyn_weight DESC"""
        )
    return {
        "weights": [
            {
                "engine": r["engine"],
                "base_weight": float(r["base_weight"]),
                "accuracy": float(r["accuracy"]) if r["accuracy"] is not None else None,
                "dyn_weight": float(r["dyn_weight"]),
                "updated_at": r["updated_at"].isoformat() if r["updated_at"] else None,
            }
            for r in rows
        ],
    }


@app.put("/engines/weights", dependencies=[Depends(require_token)])
async def put_engine_weights(body: dict = Body(...)) -> dict:
    """Retune per-engine base weights (helixa /strategy 的引擎权重编辑器等价物,更强:
    只调共识层观测权重,不碰实盘)。写 paper.engine_weights.base_weight;dyn_weight 立即
    跟随(当前无归因数据→dyn=base),EWMA 有归因后再自适应。仅编辑已存在的引擎行。"""
    updates = body.get("weights") or []
    if not isinstance(updates, list) or not updates:
        raise HTTPException(status_code=422, detail="weights: non-empty list required")
    clean: list[tuple[str, float]] = []
    for w in updates:
        eng = str(w.get("engine", "")).strip()
        try:
            bw = float(w.get("base_weight"))
        except (TypeError, ValueError):
            raise HTTPException(status_code=422, detail=f"bad base_weight for {eng!r}")
        if not eng or not (0.0 <= bw <= 5.0):
            raise HTTPException(
                status_code=422, detail=f"engine/base_weight out of range: {eng!r}={bw}"
            )
        clean.append((eng, bw))
    pool = await get_pool()
    async with pool.acquire() as conn:
        applied = []
        for eng, bw in clean:
            # only update rows that already exist (don't invent engines); dyn=base now
            res = await conn.execute(
                "UPDATE paper.engine_weights SET base_weight=$2, dyn_weight=$2, updated_at=now() WHERE engine=$1",
                eng,
                bw,
            )
            if res.endswith("1"):
                applied.append({"engine": eng, "base_weight": bw})
    return {"ok": True, "applied": applied}


# ─── /consensus/config (3O Phase 5 tuning — consensus threshold) ──────────────

_CONSENSUS_CONFIG_DDL = """
CREATE TABLE IF NOT EXISTS paper.consensus_config (
    id             INT PRIMARY KEY DEFAULT 1,
    base_threshold NUMERIC NOT NULL DEFAULT 0.45,
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT consensus_config_singleton CHECK (id = 1)
);
"""


@app.get("/consensus/config")
async def get_consensus_config() -> dict:
    """Current consensus tuning (base_threshold). Default 0.45 when unset."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(_CONSENSUS_CONFIG_DDL)
        row = await conn.fetchrow(
            "SELECT base_threshold, updated_at FROM paper.consensus_config WHERE id=1"
        )
    return {
        "base_threshold": float(row["base_threshold"]) if row else 0.45,
        "updated_at": row["updated_at"].isoformat()
        if row and row["updated_at"]
        else None,
    }


@app.put("/consensus/config", dependencies=[Depends(require_token)])
async def put_consensus_config(body: dict = Body(...)) -> dict:
    """Retune the consensus execution threshold (helixa /strategy 阈值编辑器等价物).
    consensus_adapter 下一轮读取生效。observe-only:仅改"若执行需多强共识"的判据,不下单。"""
    try:
        bt = float(body.get("base_threshold"))
    except (TypeError, ValueError):
        raise HTTPException(status_code=422, detail="base_threshold: float required")
    if not (0.1 <= bt <= 0.9):
        raise HTTPException(
            status_code=422, detail="base_threshold must be in [0.1, 0.9]"
        )
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(_CONSENSUS_CONFIG_DDL)
        await conn.execute(
            """INSERT INTO paper.consensus_config (id, base_threshold, updated_at)
               VALUES (1, $1, now())
               ON CONFLICT (id) DO UPDATE SET base_threshold=$1, updated_at=now()""",
            bt,
        )
    return {"ok": True, "base_threshold": bt}


# ─── /consensus (3O Phase 5, multi-engine ensemble) ───────────────────────────


@app.get("/consensus")
async def get_consensus() -> dict:
    """Latest multi-engine consensus per instrument (paper.consensus_signals).
    Only `promoted` engines drive `should_execute` (helivex gate discipline);
    consensus itself is observe-only until P6 wires enforcement."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        exists = await conn.fetchval("SELECT to_regclass('paper.consensus_signals')")
        if not exists:
            return {"as_of": None, "consensus": [], "weights": []}
        rows = await conn.fetch(
            """SELECT instrument, final_direction, consensus_score, kelly_position,
                      agreement_ratio, is_divergent, should_execute, n_promoted,
                      regime_state, sentiment_bias, onchain_bias, detail, cycle_ts
               FROM paper.consensus_signals
               WHERE cycle_ts = (SELECT MAX(cycle_ts) FROM paper.consensus_signals)
               ORDER BY instrument"""
        )
        wrows = await conn.fetch(
            "SELECT engine, base_weight, accuracy, dyn_weight FROM paper.engine_weights ORDER BY engine"
        )
    return {
        "as_of": rows[0]["cycle_ts"].isoformat() if rows else None,
        "consensus": [
            {
                "instrument": r["instrument"],
                "final_direction": r["final_direction"],
                "consensus_score": float(r["consensus_score"])
                if r["consensus_score"] is not None
                else None,
                "kelly_position": float(r["kelly_position"])
                if r["kelly_position"] is not None
                else None,
                "agreement_ratio": float(r["agreement_ratio"])
                if r["agreement_ratio"] is not None
                else None,
                "is_divergent": r["is_divergent"],
                "should_execute": r["should_execute"],
                "n_promoted": r["n_promoted"],
                "regime_state": r["regime_state"],
                "sentiment_bias": float(r["sentiment_bias"])
                if r["sentiment_bias"] is not None
                else None,
                "onchain_bias": float(r["onchain_bias"])
                if r["onchain_bias"] is not None
                else None,
                "detail": json.loads(r["detail"]) if r["detail"] else {},
            }
            for r in rows
        ],
        "weights": [
            {
                "engine": w["engine"],
                "base_weight": float(w["base_weight"]),
                "accuracy": float(w["accuracy"]) if w["accuracy"] is not None else None,
                "dyn_weight": float(w["dyn_weight"]),
            }
            for w in wrows
        ],
    }


# ─── /portfolio/kill ──────────────────────────────────────────────────────────


@app.post("/portfolio/kill", dependencies=[Depends(require_token)])
async def post_portfolio_kill() -> dict:
    """Stop paper node gracefully via systemd (preferred) or SIGTERM fallback.

    systemd stop: helivex-paper.service runs on_stop() then stays stopped
    (Restart=on-failure means clean SIGTERM won't bounce it back).
    """
    import signal as _signal
    import subprocess as _sub
    import os as _os

    # Try systemd first (node managed by systemd after linger migration)
    try:
        result = _sub.run(
            ["systemctl", "--user", "stop", "helivex-paper.service"],
            capture_output=True,
            text=True,
            timeout=35,
        )
        if result.returncode == 0:
            return {
                "ok": True,
                "method": "systemctl",
                "action": "helivex-paper.service stopped — on_stop() closed positions",
                "next": "restart: systemctl --user start helivex-paper.service",
            }
        # systemd failed (unit not found / not managed) — fall through to SIGTERM
    except Exception:
        pass

    # Fallback: direct SIGTERM to PID file
    pid_file = "/tmp/helivex_paper_node.pid"
    try:
        pid = int(Path(pid_file).read_text().strip())
    except (FileNotFoundError, ValueError):
        return {
            "ok": False,
            "reason": "paper node PID file not found — node may not be running",
        }

    try:
        _os.kill(pid, 0)
    except ProcessLookupError:
        return {"ok": False, "reason": f"PID {pid} not found — node already stopped"}

    try:
        _os.kill(pid, _signal.SIGTERM)
        return {
            "ok": True,
            "method": "sigterm",
            "pid": pid,
            "action": "SIGTERM sent — node will run on_stop() and close all positions",
            "next": "restart: bash paper/start_all.sh restart paper",
        }
    except PermissionError:
        return {
            "ok": False,
            "reason": f"permission denied sending SIGTERM to PID {pid}",
        }


# ─── /risk (R14 portfolio risk layer) ─────────────────────────────────────────


@app.get("/risk/status")
async def get_risk_status() -> dict:
    """Risk layer state: kill-switch, NAV/drawdown vs cap, daily P&L vs limit, caps."""
    from paper.risk import (
        DAILY_LOSS_LIMIT_USD,
        MAX_CONCURRENT_POS,
        MAX_DRAWDOWN_PCT,
        PER_INSTRUMENT_CAP,
        PER_STRATEGY_CAP,
        PORTFOLIO_GROSS_CAP,
        RISK_DDL,
        is_tripped,
        kill_switch_reason,
        nav_and_drawdown,
    )

    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(RISK_DDL)
        st = await nav_and_drawdown(conn)
    return {
        "kill_switch": {"tripped": is_tripped(), "reason": kill_switch_reason()},
        "nav": round(st["nav"], 2),
        "peak": round(st["peak"], 2),
        "drawdown_pct": round(st["dd_pct"], 3),
        "realized_all": round(st["realized_all"], 2),
        "realized_today": round(st["realized_today"], 2),
        "caps": {
            "portfolio_gross_usd": PORTFOLIO_GROSS_CAP,
            "per_strategy_usd": PER_STRATEGY_CAP,
            "per_instrument_usd": PER_INSTRUMENT_CAP,
            "max_positions": MAX_CONCURRENT_POS,
            "max_drawdown_pct": MAX_DRAWDOWN_PCT,
            "daily_loss_limit_usd": DAILY_LOSS_LIMIT_USD,
        },
    }


@app.get("/risk/events")
async def get_risk_events(limit: int = Query(30, ge=1, le=1000)) -> list[dict]:
    """Recent risk events (breach / trip / reset) from paper.risk_events."""
    from paper.risk import RISK_DDL

    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(RISK_DDL)
        rows = await conn.fetch(
            """SELECT ts, kind, entity_id, severity, message
               FROM paper.risk_events ORDER BY id DESC LIMIT $1""",
            limit,
        )
    return [
        {
            "ts": r["ts"].isoformat(),
            "kind": r["kind"],
            "entity_id": r["entity_id"],
            "severity": r["severity"],
            "message": r["message"],
        }
        for r in rows
    ]


@app.post("/risk/kill", dependencies=[Depends(require_token)])
async def post_risk_kill(body: dict = Body(default={})) -> dict:
    """Trip the soft kill-switch — halts NEW entries (exits still allowed); the node
    keeps running. Distinct from /portfolio/kill which stops the whole node."""
    from paper.risk import is_tripped, kill_switch_reason, trip

    trip(body.get("reason") or "manual trip via dashboard")
    return {"ok": True, "tripped": is_tripped(), "reason": kill_switch_reason()}


@app.post("/risk/reset", dependencies=[Depends(require_token)])
async def post_risk_reset() -> dict:
    """Clear the soft kill-switch — re-enables new entries."""
    from paper.risk import is_tripped, reset

    reset()
    return {"ok": True, "tripped": is_tripped()}


@app.post("/paper/restart", dependencies=[Depends(require_token)])
async def post_paper_restart() -> dict:
    """Restart the paper node so edited live params (Configure tab) take effect.
    The node re-reads each strategy YAML's `live` block on build."""
    import subprocess as _sub

    try:
        r = _sub.run(
            ["systemctl", "--user", "restart", "helivex-paper.service"],
            capture_output=True,
            text=True,
            timeout=45,
        )
        if r.returncode == 0:
            return {"ok": True, "message": "paper 节点已重启 — 新参数生效"}
        return {
            "ok": False,
            "reason": (r.stderr or r.stdout or "restart failed").strip(),
        }
    except Exception as e:
        return {"ok": False, "reason": str(e)}


# ─── /microstructure (R16 L2 order-book recorder) ─────────────────────────────


@app.get("/microstructure/latest")
async def get_microstructure_latest(series: int = Query(60, ge=1, le=1000)) -> dict:
    """Latest order-book features per instrument + a short imbalance/spread series
    for sparklines. Empty if the recorder has never written (table absent)."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        exists = await conn.fetchval(
            "SELECT to_regclass('market_data.orderbook_features')"
        )
        if not exists:
            return {"latest": [], "series": {}}
        latest = await conn.fetch(
            """SELECT DISTINCT ON (instrument)
                   instrument, ts, best_bid, best_ask, mid, microprice, spread_bps,
                   bid_sz1, ask_sz1, bid_depth5, ask_depth5, imbalance1, imbalance5
               FROM market_data.orderbook_features
               ORDER BY instrument, ts DESC"""
        )
        instruments = [r["instrument"] for r in latest]
        series_map: dict[str, list] = {}
        for inst in instruments:
            pts = await conn.fetch(
                """SELECT ts, mid, spread_bps, imbalance1, imbalance5
                   FROM market_data.orderbook_features
                   WHERE instrument = $1 ORDER BY ts DESC LIMIT $2""",
                inst,
                series,
            )
            series_map[inst] = [
                {
                    "ts": p["ts"].isoformat(),
                    "mid": p["mid"],
                    "spread_bps": p["spread_bps"],
                    "imbalance1": p["imbalance1"],
                    "imbalance5": p["imbalance5"],
                }
                for p in reversed(pts)
            ]
    return {
        "latest": [
            {
                "instrument": r["instrument"],
                "ts": r["ts"].isoformat(),
                "best_bid": r["best_bid"],
                "best_ask": r["best_ask"],
                "mid": r["mid"],
                "microprice": r["microprice"],
                "spread_bps": r["spread_bps"],
                "bid_sz1": r["bid_sz1"],
                "ask_sz1": r["ask_sz1"],
                "bid_depth5": r["bid_depth5"],
                "ask_depth5": r["ask_depth5"],
                "imbalance1": r["imbalance1"],
                "imbalance5": r["imbalance5"],
            }
            for r in latest
        ],
        "series": series_map,
    }


# ─── /research/ler (HELIVEX-IMPL_SPEC-LER-001, Phase R 后 — 数据积累进度) ─────

LER_PARAMS_FILE = PROJECT_ROOT / "docs" / "ler_okx_swap.params.yaml"


@app.get("/research/ler/config")
async def get_ler_config() -> dict:
    """LER 策略定义 + 预注册参数(只读)。刻意不进 STRATEGY_YAML_MAP —— 这是研究阶段
    的 spec 快照,不是可实盘/模拟盘调参的策略配置,不接 /strategies、Configure tab
    的实盘参数编辑与"保存并重启节点"流程(spec §2 明确禁止实盘/模拟盘下单)。"""
    if not LER_PARAMS_FILE.exists():
        raise HTTPException(404, "LER params file not found")
    return _read_yaml(LER_PARAMS_FILE)


async def _source_coverage(
    conn: Any, table: str, ts_col: str, venue: str, extra_where: str = ""
) -> list[dict]:
    """每 symbol 的行数/时间跨度/新鲜度,来自 marketdata 库(md schema,iris 生产)。
    extra_where 是字面量拼接的额外条件(如 timeframe 过滤),调用方硬编码传入,
    不接受外部输入 —— 不是 SQL 注入面。"""
    rows = await conn.fetch(
        f"""SELECT symbol, COUNT(*) AS n, MIN({ts_col}) AS first_ts, MAX({ts_col}) AS last_ts
            FROM {table} WHERE venue = $1 {extra_where} GROUP BY symbol ORDER BY symbol""",
        venue,
    )
    now = datetime.now(timezone.utc)
    out = []
    for r in rows:
        first_ts, last_ts = r["first_ts"], r["last_ts"]
        out.append(
            {
                "symbol": r["symbol"],
                "rows": int(r["n"]),
                "first_ts": first_ts.isoformat() if first_ts else None,
                "last_ts": last_ts.isoformat() if last_ts else None,
                "days_covered": round((last_ts - first_ts).total_seconds() / 86400, 2)
                if first_ts and last_ts
                else 0.0,
                "freshness_minutes": round((now - last_ts).total_seconds() / 60, 1)
                if last_ts
                else None,
            }
        )
    return out


@app.get("/research/ler/coverage")
async def get_ler_coverage() -> dict:
    """LER(HELIVEX-IMPL_SPEC-LER-001)数据积累进度面板 —— Phase R 后数据源改为 OKX
    (docs/HELIVEX-IMPL_SPEC-LER-001.md §3/§12)。读 marketdata 库的 md schema(iris
    直接生产),不是 helivex 自己的 market_data schema(那批 adapter 还没覆盖
    liquidations/OI/1m OHLCV)。诚实展示原始覆盖 —— T1∧T2 触发 episode 计数需要
    oskill 检测逻辑(尚未实现),不在此处伪造。
    """
    md_pool = await get_md_pool()
    async with md_pool.acquire() as conn:
        sources = {
            "liquidations": await _source_coverage(
                conn, "md.liquidations", "ts", "okx"
            ),
            # md.ohlcv 混了 h4/m5/m1 多个 timeframe;LER 信号层要 1m,单独按 timeframe 过滤。
            "ohlcv_1m": await _source_coverage(
                conn,
                "md.ohlcv",
                "bar_open_ts",
                "okx",
                extra_where="AND timeframe = 'm1' AND instrument_type = 'perp'",
            ),
            "funding": await _source_coverage(
                conn, "md.funding_settled", "funding_time", "okx"
            ),
            "oi": await _source_coverage(conn, "md.oi", "ts", "okx"),
        }

    return {
        "as_of": datetime.now(timezone.utc).isoformat(),
        "venue": "okx",
        "sources": sources,
        "v3_threshold": {
            "required_n_trades_per_config": 100,
            "n_configs": 8,
            "note": (
                "T1∧T2(爆仓瀑布)触发 episode 计数依赖 oskill 检测逻辑,尚未实现 —— "
                "以上只是原始行情/强平/funding/OI 覆盖,不代表已有可用信号样本。"
                "预期积累到统计意义上足够的 episode 数是月级时间尺度,见 spec §4/§12.3。"
            ),
        },
    }


# ─── Health ───────────────────────────────────────────────────────────────────


@app.get("/health")
async def health() -> dict:
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.fetchval("SELECT 1")
    return {"ok": True, "ts": datetime.now(timezone.utc).isoformat()}
