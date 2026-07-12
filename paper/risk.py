"""paper.risk — portfolio-level pre-trade risk gate + circuit breakers + kill-switch.

This is the layer that protects the paper book *regardless of alpha*. Two halves:

  1. PRE-TRADE GATE (in-process, deterministic) — `RISK.gate_entry(...)`.
     Each strategy calls it before submitting an ENTRY order. It enforces hard
     caps on portfolio gross exposure, per-strategy exposure, per-instrument
     concentration, and max concurrent positions, and refuses ALL new entries
     when the kill-switch is tripped. Exits are never blocked (always de-risk).

  2. CIRCUIT BREAKERS (out-of-process, periodic) — evaluators run by the same
     AlerterEngine as the health checks (paper/alerter.py). They read realized
     P&L from paper.fills and TRIP THE KILL-SWITCH on a drawdown / daily-loss
     breach. Tripping is cross-process via a file flag, so the breaker (monitor
     process) can halt the node process's new entries.

Design choices:
  - The exposure registry is per-process and in-memory. The whole paper book runs
    in ONE TradingNode process (paper/node.py), so a module-level singleton sees
    every strategy's open notional. The kill-switch FILE is the cross-process
    channel to the monitor.
  - gate_entry NEVER raises — on any internal error it fails OPEN (allow + log),
    because this is paper and a risk-module bug must not silently halt trading.
    The kill-switch is the one hard, explicit stop.
  - The drawdown breaker uses REALIZED P&L (closed round-trips, avg-cost matched).
    It does not mark open positions to market — a documented v1 limitation; a
    large *unrealized* drawdown is caught only once positions close or via the
    daily-loss limit. Tune caps via env; see CONFIG below.

CLI:
  python -m paper.risk status     # show caps, exposure, kill-switch, NAV
  python -m paper.risk trip "msg"  # manually trip the kill-switch
  python -m paper.risk reset       # clear the kill-switch
  python -m paper.risk initdb      # create paper.risk_events table
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import logging
import os
import threading
from dataclasses import dataclass, field
from pathlib import Path

import asyncpg

from paper.db import DB_DSN

log = logging.getLogger(__name__)

# ── CONFIG (env-overridable; generous defaults sized for the 5k demo book) ──────
BASE_EQUITY_USD = float(os.environ.get("HELIVEX_BASE_EQUITY_USD", "5000"))
PORTFOLIO_GROSS_CAP = float(os.environ.get("HELIVEX_PORTFOLIO_GROSS_CAP_USD", "3000"))
PER_STRATEGY_CAP = float(os.environ.get("HELIVEX_PER_STRATEGY_CAP_USD", "1200"))
PER_INSTRUMENT_CAP = float(os.environ.get("HELIVEX_PER_INSTRUMENT_CAP_USD", "1000"))
MAX_CONCURRENT_POS = int(os.environ.get("HELIVEX_MAX_CONCURRENT_POSITIONS", "12"))
MAX_DRAWDOWN_PCT = float(
    os.environ.get("HELIVEX_MAX_DRAWDOWN_PCT", "15")
)  # of peak NAV
DAILY_LOSS_LIMIT_USD = float(os.environ.get("HELIVEX_DAILY_LOSS_LIMIT_USD", "250"))

# CVaR/dynamic-risk phase 1 (3O port, see docs plan curried-mixing-crab.md):
# 0 = manual reset only (default, unchanged behavior). >0 = auto-clear the
# kill-switch after this many hours untouched, matching helixa's circuit-breaker
# auto-reset. Its own independent rollout stage — off until enabled deliberately.
KILL_SWITCH_AUTO_RESET_HOURS = float(
    os.environ.get("HELIVEX_KILL_SWITCH_AUTO_RESET_HOURS", "0")
)
# observe (default, Stage A) = compute + log only, never tightens gate_entry's
# real decision. tier1 / tier1+2 / all = Stage B sub-stages (see gate_entry_dynamic).
DYNAMIC_RISK_ENFORCE = os.environ.get("HELIVEX_DYNAMIC_RISK_ENFORCE", "observe")

# Default lives in the repo (mounted at /app in BOTH the paper node and gateway
# containers), so a trip from the gateway's /risk/kill and a trip from the paper
# node write the SAME physical file the node's gate_entry reads. The old /tmp
# default was container-local — /tmp is not shared — so the dashboard kill button
# wrote a file the node never read (a silent no-op). Env override still honored.
_REPO_ROOT = Path(__file__).resolve().parent.parent
KILL_SWITCH_FILE = Path(
    os.environ.get(
        "HELIVEX_KILL_SWITCH_FILE", str(_REPO_ROOT / "run" / "helivex_paper_killswitch")
    )
)
# Persistent high-water-mark for drawdown. The old peak proxy `max(base, nav)`
# recomputed from current realized P&L every call, so it could NEVER show a
# drawdown from a prior equity high (a +500 peak that fell to +300 reported dd=0).
# A ratcheting HWM on disk fixes that and survives restarts.
# Same shared-mount rationale as KILL_SWITCH_FILE: the paper node ratchets the HWM
# and the gateway's /risk/status reads it to report drawdown. A container-local
# /tmp gave the gateway no HWM (peak fell back to base), under-reporting drawdown.
HWM_FILE = Path(
    os.environ.get("HELIVEX_HWM_FILE", str(_REPO_ROOT / "run" / "helivex_paper_hwm"))
)


def _read_hwm(default: float) -> float:
    try:
        return max(default, float(HWM_FILE.read_text().strip()))
    except (OSError, ValueError):
        return default


def _write_hwm(value: float) -> None:
    try:
        HWM_FILE.parent.mkdir(parents=True, exist_ok=True)
        HWM_FILE.write_text(f"{value:.6f}\n")
    except OSError:
        pass


@dataclass(frozen=True)
class RiskDecision:
    allowed: bool
    reason: str = ""
    tiers: dict | None = None


# ── kill-switch (cross-process via file flag) ───────────────────────────────────


def is_tripped() -> bool:
    if not KILL_SWITCH_FILE.exists():
        return False
    if KILL_SWITCH_AUTO_RESET_HOURS > 0 and _auto_reset_due():
        reset()
        log.warning(
            "kill-switch auto-reset after %.1fh (HELIVEX_KILL_SWITCH_AUTO_RESET_HOURS)",
            KILL_SWITCH_AUTO_RESET_HOURS,
        )
        return False
    return True


def _auto_reset_due() -> bool:
    """True if the kill-switch has been tripped longer than the auto-reset window.
    Reads the trip timestamp already written by trip() — no new file format."""
    try:
        raw = KILL_SWITCH_FILE.read_text().strip()
        ts_str = raw.split("\t", 1)[0]
        tripped_at = _dt.datetime.fromisoformat(ts_str)
        elapsed_hours = (
            _dt.datetime.now(_dt.timezone.utc) - tripped_at
        ).total_seconds() / 3600
        return elapsed_hours >= KILL_SWITCH_AUTO_RESET_HOURS
    except (OSError, ValueError, IndexError):
        return False


def trip(reason: str) -> None:
    """Trip the kill-switch. Idempotent — keeps the first trip's reason/time."""
    if KILL_SWITCH_FILE.exists():
        return
    ts = _dt.datetime.now(_dt.timezone.utc).isoformat()
    KILL_SWITCH_FILE.parent.mkdir(parents=True, exist_ok=True)
    KILL_SWITCH_FILE.write_text(f"{ts}\t{reason}\n")
    log.critical("KILL-SWITCH TRIPPED: %s", reason)


def reset() -> None:
    KILL_SWITCH_FILE.unlink(missing_ok=True)
    log.warning("kill-switch reset (cleared)")


def kill_switch_reason() -> str:
    if not KILL_SWITCH_FILE.exists():
        return ""
    try:
        return KILL_SWITCH_FILE.read_text().strip()
    except OSError:
        return "(unreadable)"


# ── in-process exposure registry + pre-trade gate ───────────────────────────────


@dataclass
class PortfolioRiskManager:
    """Tracks open notional per (strategy_id, instrument) within this process."""

    _exposure: dict[tuple[str, str], float] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    # — bookkeeping (called by strategies on entry pass / exit) —
    def open_position(
        self, strategy_id: str, instrument: str, notional_usd: float
    ) -> None:
        with self._lock:
            self._exposure[(strategy_id, instrument)] = abs(notional_usd)

    def close_position(self, strategy_id: str, instrument: str) -> None:
        with self._lock:
            self._exposure.pop((strategy_id, instrument), None)

    def sync_external_exposure(self, by_inst: dict[str, float]) -> None:
        """账户级敞口对账:cache 全量持仓美元敞口 − 策略级账本已计部分 =
        外部残留(重启后账本清零而 venue 仓位仍在的缺口),记 ("external", inst)。
        策略级归因不可恢复(重启后仓位是 EXTERNAL,不带 strategy_id),此对账
        只保证 gross / per-instrument cap 面对真实敞口。"""
        with self._lock:
            for key in [k for k in self._exposure if k[0] == "external"]:
                self._exposure.pop(key, None)
            for inst, total in by_inst.items():
                tracked = sum(v for (s, i), v in self._exposure.items() if i == inst)
                extra = total - tracked
                if extra > 1e-9:
                    self._exposure[("external", inst)] = extra

    # — views —
    def gross_exposure(self) -> float:
        with self._lock:
            return sum(self._exposure.values())

    def strategy_exposure(self, strategy_id: str) -> float:
        with self._lock:
            return sum(v for (s, _), v in self._exposure.items() if s == strategy_id)

    def instrument_exposure(self, instrument: str) -> float:
        with self._lock:
            return sum(v for (_, i), v in self._exposure.items() if i == instrument)

    def n_positions(self) -> int:
        with self._lock:
            return len(self._exposure)

    def snapshot(self) -> dict:
        with self._lock:
            exp = dict(self._exposure)
        return {
            "gross": sum(exp.values()),
            "n_positions": len(exp),
            "by_strategy": _agg(exp, 0),
            "by_instrument": _agg(exp, 1),
        }

    # — the pre-trade gate —
    def gate_entry(
        self, strategy_id: str, instrument: str, notional_usd: float
    ) -> RiskDecision:
        """Decide whether a NEW entry of `notional_usd` is allowed. Never raises."""
        try:
            if is_tripped():
                return RiskDecision(
                    False, f"kill-switch tripped: {kill_switch_reason()}"
                )

            notional = abs(notional_usd)
            # treat re-entry on an already-open (strategy,instrument) as a replace, not an add
            with self._lock:
                cur_key = self._exposure.get((strategy_id, instrument), 0.0)
                gross = sum(self._exposure.values()) - cur_key + notional
                strat = (
                    sum(v for (s, _), v in self._exposure.items() if s == strategy_id)
                    - cur_key
                    + notional
                )
                instr = (
                    sum(v for (_, i), v in self._exposure.items() if i == instrument)
                    - cur_key
                    + notional
                )
                n_pos = len(self._exposure) + (
                    0 if (strategy_id, instrument) in self._exposure else 1
                )

            if gross > PORTFOLIO_GROSS_CAP:
                return RiskDecision(
                    False,
                    f"portfolio gross {gross:.0f} > cap {PORTFOLIO_GROSS_CAP:.0f}",
                )
            if strat > PER_STRATEGY_CAP:
                return RiskDecision(
                    False,
                    f"strategy {strategy_id} exposure {strat:.0f} > cap {PER_STRATEGY_CAP:.0f}",
                )
            if instr > PER_INSTRUMENT_CAP:
                return RiskDecision(
                    False,
                    f"instrument {instrument} exposure {instr:.0f} > cap {PER_INSTRUMENT_CAP:.0f}",
                )
            if n_pos > MAX_CONCURRENT_POS:
                return RiskDecision(
                    False, f"open positions {n_pos} > cap {MAX_CONCURRENT_POS}"
                )
            return RiskDecision(True, "ok")
        except Exception as e:  # fail-open (paper): never let a risk bug halt trading
            log.error("gate_entry internal error (failing open): %s", e)
            return RiskDecision(True, f"fail-open ({e})")


def _agg(exp: dict[tuple[str, str], float], idx: int) -> dict[str, float]:
    out: dict[str, float] = {}
    for k, v in exp.items():
        out[k[idx]] = out.get(k[idx], 0.0) + v
    return out


# module-level singleton — one paper book per process
RISK = PortfolioRiskManager()


# ── dynamic risk caps (CVaR/ATR/correlation, from cvar_risk_adapter.py) ─────────
# Phase 1 of the 3O helixa-capability port (see plan curried-mixing-crab.md).
# Populated hourly by ops/scripts/cvar_risk_adapter.py into paper.position_caps;
# refreshed here into an in-process cache by eval_refresh_dynamic_caps
# (paper/alerter.py). Stage A (DYNAMIC_RISK_ENFORCE="observe", default): computed
# and logged only — gate_entry_dynamic always defers to gate_entry's real
# allow/deny and never tightens it. Stage B sub-stages ("tier1"/"tier1+2"/"all")
# progressively let the dynamic caps additionally restrict new entries.

DYNAMIC_CAPS: dict[str, dict] = {}
_dynamic_caps_lock = threading.Lock()


async def refresh_dynamic_caps(conn: asyncpg.Connection) -> None:
    """Load the latest paper.position_caps cycle into the in-process cache."""
    rows = await conn.fetch(
        """
        SELECT instrument, tier1_headroom, tier2_atr_cap, tier3_corr_clip,
               effective_cap_usd, binding_tier, cycle_ts
        FROM paper.position_caps
        WHERE cycle_ts = (SELECT MAX(cycle_ts) FROM paper.position_caps)
        """
    )
    caps = {
        r["instrument"]: {
            "tier1_headroom": float(r["tier1_headroom"]),
            "tier2_atr_cap": float(r["tier2_atr_cap"]),
            "tier3_corr_clip": float(r["tier3_corr_clip"]),
            "effective_cap_usd": float(r["effective_cap_usd"]),
            "binding_tier": r["binding_tier"],
            "cycle_ts": r["cycle_ts"],
        }
        for r in rows
    }
    with _dynamic_caps_lock:
        DYNAMIC_CAPS.clear()
        DYNAMIC_CAPS.update(caps)


def gate_entry_dynamic(
    strategy_id: str, instrument: str, notional_usd: float
) -> RiskDecision:
    """Static gate_entry, optionally tightened further by the dynamic caps cache.

    Never loosens the static decision — if gate_entry already rejects, that
    decision is final. Only when it allows does this consult DYNAMIC_CAPS,
    and only actually shrinks the effective size when
    HELIVEX_DYNAMIC_RISK_ENFORCE opts into a tier (Stage B). In the default
    "observe" mode it computes what it WOULD have decided (visible via
    `tiers` on the returned RiskDecision) without changing `allowed`.
    """
    static = RISK.gate_entry(strategy_id, instrument, notional_usd)
    if not static.allowed:
        return static

    try:
        with _dynamic_caps_lock:
            caps = DYNAMIC_CAPS.get(instrument)
        if caps is None:
            return static  # no cycle yet (cold start) — static gate stands alone

        enforce = DYNAMIC_RISK_ENFORCE
        tiers_to_check = {
            "tier1": {"tier1_headroom"},
            "tier1+2": {"tier1_headroom", "tier2_atr_cap"},
            "all": {"tier1_headroom", "tier2_atr_cap", "tier3_corr_clip"},
        }.get(enforce, set())

        active_caps = [caps[t] for t in tiers_to_check if t in caps]
        effective_cap = min(active_caps) if active_caps else float("inf")

        would_allow = abs(notional_usd) <= effective_cap
        if enforce == "observe" or would_allow:
            return RiskDecision(static.allowed, static.reason, tiers=caps)

        return RiskDecision(
            False,
            f"dynamic cap ({enforce}): {abs(notional_usd):.0f} > "
            f"effective {effective_cap:.0f} (binding_tier={caps['binding_tier']})",
            tiers=caps,
        )
    except Exception as e:  # fail-open, same posture as gate_entry itself
        log.error("gate_entry_dynamic internal error (failing open): %s", e)
        return static


# ── realized P&L (avg-cost round-trip matching) for the breakers ────────────────


async def realized_pnl(
    conn: asyncpg.Connection, since: _dt.datetime | None = None
) -> float:
    """Cumulative realized P&L (USD) from closed round-trips, avg-cost matched.

    Walks fills in ts order per (strategy_id, instrument); realizes P&L on the
    reducing side. Open positions contribute nothing (conservative). `since`
    filters fills by ts (None = all-time).
    """
    where = "WHERE ts >= $1" if since else ""
    args = [since] if since else []
    rows = await conn.fetch(
        f"""SELECT strategy_id, instrument, side, quantity, actual_fill_price
            FROM paper.fills {where} ORDER BY ts ASC""",
        *args,
    )
    # position state per key: (signed_qty, avg_cost)
    pos: dict[tuple[str, str], list[float]] = {}
    pnl = 0.0
    for r in rows:
        key = (r["strategy_id"], r["instrument"])
        q, cost = pos.get(key, [0.0, 0.0])
        fill_q = float(r["quantity"]) * (1.0 if r["side"] == "BUY" else -1.0)
        px = float(r["actual_fill_price"])
        if q == 0 or (q > 0) == (fill_q > 0):
            # opening or adding to the position → update avg cost
            new_q = q + fill_q
            cost = (
                (cost * abs(q) + px * abs(fill_q)) / abs(new_q) if new_q != 0 else 0.0
            )
            q = new_q
        else:
            # reducing / closing → realize on the closed amount
            closed = min(abs(fill_q), abs(q))
            direction = 1.0 if q > 0 else -1.0
            pnl += direction * (px - cost) * closed
            q += fill_q
            if q == 0:
                cost = 0.0
            elif (q > 0) != (q - fill_q > 0):
                # flipped through zero → remainder opens at fill price
                cost = px
        pos[key] = [q, cost]
    return pnl


async def open_positions(
    conn: asyncpg.Connection,
) -> dict[tuple[str, str], list[float]]:
    """Current open book per (strategy_id, instrument) as [signed_qty, avg_cost],
    from the same avg-cost walk as realized_pnl. Flat keys omitted."""
    rows = await conn.fetch(
        """SELECT strategy_id, instrument, side, quantity, actual_fill_price
           FROM paper.fills ORDER BY ts ASC"""
    )
    pos: dict[tuple[str, str], list[float]] = {}
    for r in rows:
        key = (r["strategy_id"], r["instrument"])
        q, cost = pos.get(key, [0.0, 0.0])
        fill_q = float(r["quantity"]) * (1.0 if r["side"] == "BUY" else -1.0)
        px = float(r["actual_fill_price"])
        if q == 0 or (q > 0) == (fill_q > 0):
            new_q = q + fill_q
            cost = (
                (cost * abs(q) + px * abs(fill_q)) / abs(new_q) if new_q != 0 else 0.0
            )
            q = new_q
        else:
            q += fill_q
            if q == 0:
                cost = 0.0
            elif (q > 0) != (q - fill_q > 0):
                cost = px
        pos[key] = [q, cost]
    return {k: v for k, v in pos.items() if abs(v[0]) > 1e-12}


async def _latest_marks(
    conn: asyncpg.Connection, fresh_seconds: int = 600
) -> dict[str, float]:
    """Latest fresh mid price per instrument from the L2 recorder feed.

    The orderbook_features table only exists once the L2 recorder has run. If it
    is absent (fresh host, recorder never started) return no marks rather than
    raising UndefinedTableError — otherwise every nav/drawdown/risk-status caller
    500s. Positions then contribute 0 unrealized (conservative), same as an
    unmarked instrument.
    """
    exists = await conn.fetchval("SELECT to_regclass('market_data.orderbook_features')")
    if exists is None:
        return {}
    rows = await conn.fetch(
        f"""SELECT DISTINCT ON (instrument) instrument, mid
            FROM market_data.orderbook_features
            WHERE ts > now() - interval '{int(fresh_seconds)} seconds' AND mid IS NOT NULL
            ORDER BY instrument, ts DESC"""
    )
    return {r["instrument"]: float(r["mid"]) for r in rows}


async def unrealized_pnl(conn: asyncpg.Connection) -> dict:
    """Mark-to-market P&L of the open book using fresh L2 mids. Positions without a
    fresh mark (e.g. spot instruments) contribute 0 and are counted (conservative)."""
    positions = await open_positions(conn)
    marks = await _latest_marks(conn)
    upnl = 0.0
    marked = unmarked = 0
    for (_strat, inst), (qty, cost) in positions.items():
        mark = marks.get(inst)
        if mark is None:
            unmarked += 1
            continue
        upnl += (
            mark - cost
        ) * qty  # qty is signed: short positions profit as mark falls
        marked += 1
    return {"unrealized": upnl, "n_marked": marked, "n_unmarked": unmarked}


async def nav_and_drawdown(conn: asyncpg.Connection) -> dict:
    """Mark-to-market NAV = base + realized + unrealized. Returns nav, HWM peak,
    dd_pct (now sees OPEN-position losses), realized today/all, unrealized."""
    all_time = await realized_pnl(conn)
    midnight = _dt.datetime.now(_dt.timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    today = await realized_pnl(conn, since=midnight)
    u = await unrealized_pnl(conn)
    nav = BASE_EQUITY_USD + all_time + u["unrealized"]
    # Ratcheting high-water-mark: peak = max(ever-seen NAV, current NAV). Persisted
    # so drawdown is measured from the true equity high, not reset each call.
    peak = _read_hwm(BASE_EQUITY_USD)
    if nav > peak:
        peak = nav
        _write_hwm(peak)
    dd_pct = (peak - nav) / peak * 100.0 if peak > 0 else 0.0
    return {
        "nav": nav,
        "peak": peak,
        "dd_pct": dd_pct,
        "realized_all": all_time,
        "realized_today": today,
        "unrealized": u["unrealized"],
        "n_unmarked": u["n_unmarked"],
    }


# ── DB audit of risk events ─────────────────────────────────────────────────────

RISK_DDL = """
CREATE TABLE IF NOT EXISTS paper.risk_events (
    id          BIGSERIAL PRIMARY KEY,
    ts          TIMESTAMPTZ NOT NULL DEFAULT now(),
    kind        TEXT NOT NULL,          -- 'breach' | 'trip' | 'reset' | 'block'
    entity_id   TEXT NOT NULL,
    severity    TEXT NOT NULL,
    message     TEXT NOT NULL,
    metrics     JSONB
);
"""


async def log_risk_event(
    conn: asyncpg.Connection,
    kind: str,
    entity_id: str,
    severity: str,
    message: str,
    metrics: dict | None = None,
) -> None:
    import json

    await conn.execute(
        """INSERT INTO paper.risk_events (kind, entity_id, severity, message, metrics)
           VALUES ($1,$2,$3,$4,$5)""",
        kind,
        entity_id,
        severity,
        message,
        json.dumps(metrics) if metrics else None,
    )


# ── circuit-breaker evaluators (AlerterEngine-compatible) ───────────────────────
# signature: async evaluator(*, config=None) -> list[dict{entity_id,severity,message}]


def _alert(entity_id: str, severity: str, message: str) -> dict:
    return {"entity_id": entity_id, "severity": severity, "message": message}


async def eval_portfolio_drawdown(*, config: dict | None = None) -> list[dict]:
    """Trip the kill-switch if realized NAV drawdown exceeds the cap."""
    cfg = config or {}
    cap = float(cfg.get("max_drawdown_pct", MAX_DRAWDOWN_PCT))
    try:
        conn = await asyncpg.connect(DB_DSN)
        try:
            await conn.execute(RISK_DDL)
            st = await nav_and_drawdown(conn)
            if st["dd_pct"] >= cap:
                msg = (
                    f"portfolio drawdown {st['dd_pct']:.1f}% >= {cap:.1f}% "
                    f"(NAV {st['nav']:.0f} / peak {st['peak']:.0f}) — tripping kill-switch"
                )
                if not is_tripped():
                    trip(msg)
                    await log_risk_event(
                        conn, "trip", "portfolio_drawdown", "critical", msg, st
                    )
                return [_alert("portfolio_drawdown", "critical", msg)]
            return []
        finally:
            await conn.close()
    except Exception as e:
        return [_alert("portfolio_drawdown", "high", f"breaker eval failed: {e}")]


async def eval_daily_loss(*, config: dict | None = None) -> list[dict]:
    """Trip the kill-switch if today's realized loss exceeds the daily limit."""
    cfg = config or {}
    limit = float(cfg.get("daily_loss_limit_usd", DAILY_LOSS_LIMIT_USD))
    try:
        conn = await asyncpg.connect(DB_DSN)
        try:
            await conn.execute(RISK_DDL)
            st = await nav_and_drawdown(conn)
            if st["realized_today"] <= -abs(limit):
                msg = (
                    f"daily realized loss {st['realized_today']:.0f} <= -{abs(limit):.0f} "
                    f"— tripping kill-switch"
                )
                if not is_tripped():
                    trip(msg)
                    await log_risk_event(
                        conn, "trip", "daily_loss", "critical", msg, st
                    )
                return [_alert("daily_loss", "critical", msg)]
            return []
        finally:
            await conn.close()
    except Exception as e:
        return [_alert("daily_loss", "high", f"breaker eval failed: {e}")]


_last_dynamic_caps_refresh: _dt.datetime | None = None


async def eval_refresh_dynamic_caps(*, config: dict | None = None) -> list[dict]:
    """Refresh DYNAMIC_CAPS from paper.position_caps, self-throttled against the
    adapter's own ~hourly cadence (this evaluator runs every 120s like the rest
    of AlerterEngine, but re-querying that often would be wasted work)."""
    global _last_dynamic_caps_refresh
    cfg = config or {}
    min_interval_s = float(cfg.get("min_interval_seconds", 300))
    now = _dt.datetime.now(_dt.timezone.utc)
    if (
        _last_dynamic_caps_refresh is not None
        and (now - _last_dynamic_caps_refresh).total_seconds() < min_interval_s
    ):
        return []
    try:
        conn = await asyncpg.connect(DB_DSN)
        try:
            await refresh_dynamic_caps(conn)
        finally:
            await conn.close()
        _last_dynamic_caps_refresh = now
        return []
    except Exception as e:
        return [_alert("dynamic_caps_refresh", "low", f"refresh failed: {e}")]


# ── CLI ─────────────────────────────────────────────────────────────────────────


async def _cli_status() -> None:
    conn = await asyncpg.connect(DB_DSN)
    try:
        await conn.execute(RISK_DDL)
        st = await nav_and_drawdown(conn)
    finally:
        await conn.close()
    print("── helivex paper risk ──────────────────────────────────────────")
    print(
        f"kill-switch : {'TRIPPED — ' + kill_switch_reason() if is_tripped() else 'clear'}"
    )
    print(
        f"NAV         : {st['nav']:.2f}  (base {BASE_EQUITY_USD:.0f} + realized {st['realized_all']:+.2f} "
        f"+ unrealized {st['unrealized']:+.2f}; {st['n_unmarked']} unmarked)"
    )
    print(
        f"drawdown    : {st['dd_pct']:.2f}%  (peak {st['peak']:.0f})   cap {MAX_DRAWDOWN_PCT:.0f}%"
    )
    print(
        f"today P&L   : {st['realized_today']:+.2f}   daily limit -{DAILY_LOSS_LIMIT_USD:.0f}"
    )
    print(
        "caps        : "
        f"gross {PORTFOLIO_GROSS_CAP:.0f} | per-strat {PER_STRATEGY_CAP:.0f} | "
        f"per-instr {PER_INSTRUMENT_CAP:.0f} | max-pos {MAX_CONCURRENT_POS}"
    )
    print("(in-process exposure registry is per-node-process; not visible from CLI)")


def main() -> None:
    import sys

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "status":
        asyncio.run(_cli_status())
    elif cmd == "trip":
        trip(sys.argv[2] if len(sys.argv) > 2 else "manual trip via CLI")
        print("kill-switch tripped:", kill_switch_reason())
    elif cmd == "reset":
        reset()
        print("kill-switch cleared")
    elif cmd == "initdb":

        async def _init():
            conn = await asyncpg.connect(DB_DSN)
            await conn.execute(RISK_DDL)
            await conn.close()
            print("paper.risk_events ensured")

        asyncio.run(_init())
    else:
        print(
            f"unknown command: {cmd}\nusage: python -m paper.risk [status|trip <msg>|reset|initdb]"
        )


if __name__ == "__main__":
    main()
