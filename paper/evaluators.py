"""paper.evaluators — health evaluators injected into AlerterEngine.

Four oprim evaluators, each returns list[dict] (empty = healthy):
  eval_node_alive    — paper/run.py PID alive?
  eval_ws_tick_flow  — OKX WS ticks flowing? (via signal recency in DB)
  eval_audit_chain   — Ed25519 sig_b64 populated on recent signals?
  eval_on_bar_trigger — on_bar fired within lag budget after last bar close?

AlerterEngine calls: evaluator(config=evaluator_config) → list[dict]
Each alert event must contain: entity_id, severity, message.
"""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

import asyncpg

from paper.db import DB_DSN

log = logging.getLogger(__name__)

# Shared default: where run.py writes the PID
PAPER_PID_FILE = Path("/tmp/helivex_paper_node.pid")


def _maybe_restart(service: str, cooldown_s: float = 600.0) -> bool:
    """Auto-remediation: `systemctl --user restart <service>`, rate-limited by a
    cooldown file so a persistent fault can't become a restart loop. Returns True
    if a restart was issued. Used ONLY for data-only collectors — the trading
    node is left alert-only so a human decides on a disruptive bounce.
    """
    stamp = Path(f"/tmp/helivex_restart_{service}.stamp")
    try:
        if stamp.exists() and (time.time() - stamp.stat().st_mtime) < cooldown_s:
            return False
        stamp.write_text(str(time.time()))
        subprocess.run(
            ["systemctl", "--user", "restart", service],
            check=False,
            timeout=30,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        log.warning("auto-remediation: restarted %s", service)
        return True
    except Exception as exc:
        log.error("auto-remediation failed for %s: %s", service, exc)
        return False


# ── 1. process liveness ───────────────────────────────────────────────────────


async def _liveness_from_signals(cfg: dict) -> list[dict]:
    """DB-driven liveness proxy: a node that is persisting signals is alive.

    Used when no PID file is visible to the monitor (containerised deploy — the
    node's PID namespace differs, so os.kill can't see it). A fresh paper.signals
    row is unambiguous proof of life; staleness/emptiness is the alert.
    """
    stale_s: float = cfg.get("liveness_stale_seconds", 15 * 60)
    try:
        conn = await asyncpg.connect(DB_DSN)
        row = await conn.fetchrow("SELECT MAX(ts) AS last_ts FROM paper.signals")
        await conn.close()
    except Exception as e:
        return [
            _alert("paper_node_pid", "high", f"DB error checking node liveness: {e}")
        ]
    if row is None or row["last_ts"] is None:
        return [
            _alert(
                "paper_node_pid",
                "critical",
                "no PID file and paper.signals empty — node not started or never wrote",
            )
        ]
    age = (datetime.now(timezone.utc) - row["last_ts"]).total_seconds()
    if age > stale_s:
        return [
            _alert(
                "paper_node_pid",
                "critical",
                f"no PID file and no signal in {age / 60:.0f}min "
                f"(threshold {stale_s / 60:.0f}min) — node down or write path dead",
            )
        ]
    return []


async def eval_node_alive(*, config: dict | None = None) -> list[dict]:
    """Check that the paper node is alive — by PID when visible, else by DB writes."""
    cfg = config or {}
    pid_file = Path(cfg.get("pid_file", str(PAPER_PID_FILE)))

    # No visible PID file (fresh /tmp, or monitor in a separate container/PID
    # namespace): fall back to signal-freshness rather than false-alarm.
    if not pid_file.exists():
        return await _liveness_from_signals(cfg)

    try:
        pid = int(pid_file.read_text().strip())
    except (ValueError, OSError) as e:
        return [_alert("paper_node_pid", "high", f"Cannot read PID file: {e}")]

    try:
        os.kill(pid, 0)
        return []  # alive
    except ProcessLookupError:
        return [
            _alert(
                "paper_node_pid",
                "critical",
                f"Process PID={pid} not found — node has died",
            )
        ]
    except PermissionError:
        return []  # process exists (different uid) — treat as alive


# ── 2. OKX WS tick flow ───────────────────────────────────────────────────────


async def eval_ws_tick_flow(*, config: dict | None = None) -> list[dict]:
    """OKX WS liveness via signal recency.

    VwapMR1H fires on_bar every hour; stale > stale_seconds → WS dead.
    Default threshold: 70 min (1H bar period + 10 min buffer).
    """
    cfg = config or {}
    stale_s: float = cfg.get("stale_seconds", 70 * 60)

    try:
        conn = await asyncpg.connect(DB_DSN)
        row = await conn.fetchrow("SELECT MAX(ts) AS last_ts FROM paper.signals")
        await conn.close()
    except Exception as e:
        return [_alert("okx_ws_tick", "high", f"DB error checking tick flow: {e}")]

    if row is None or row["last_ts"] is None:
        return [
            _alert(
                "okx_ws_tick",
                "high",
                "paper.signals is empty — node never connected to OKX WS or DB unavailable",
            )
        ]

    age = (datetime.now(timezone.utc) - row["last_ts"]).total_seconds()
    if age > stale_s:
        return [
            _alert(
                "okx_ws_tick",
                "critical",
                f"No OKX signal in {age / 60:.0f}min (threshold {stale_s / 60:.0f}min) "
                f"— OKX WS disconnected?",
            )
        ]
    return []


# ── 3. audit chain integrity ──────────────────────────────────────────────────


async def eval_audit_chain(*, config: dict | None = None) -> list[dict]:
    """Check that recent signals carry a non-empty Ed25519 sig_b64 (GOLD tier).

    If HELIVEX_AUDIT_PRIVATE_KEY_B64 is set, we're in GOLD tier and every
    signal should have sig_b64 populated.  An empty sig_b64 means the audit
    module silently fell back to STANDARD — fire alert.
    """
    cfg = config or {}
    n: int = cfg.get("verify_n", 5)

    # Only meaningful in GOLD tier
    if not os.environ.get("HELIVEX_AUDIT_PRIVATE_KEY_B64", ""):
        return []  # STANDARD tier — no signing expected

    try:
        conn = await asyncpg.connect(DB_DSN)
        rows = await conn.fetch(
            "SELECT id, strategy_id, fingerprint_hex, sig_b64 "
            "FROM paper.signals ORDER BY ts DESC LIMIT $1",
            n,
        )
        await conn.close()
    except Exception as e:
        return [_alert("audit_chain", "high", f"DB error checking audit chain: {e}")]

    if not rows:
        return []  # no signals yet

    unsigned = [r["strategy_id"] for r in rows if not r["sig_b64"]]
    no_fp = [r["strategy_id"] for r in rows if not r["fingerprint_hex"]]

    problems = []
    if unsigned:
        problems.append(
            _alert(
                "audit_chain",
                "high",
                f"GOLD tier but sig_b64 empty on {len(unsigned)} recent signals "
                f"({unsigned[:3]}) — Ed25519 signing degraded to STANDARD?",
            )
        )
    if no_fp:
        problems.append(
            _alert(
                "audit_chain",
                "high",
                f"fingerprint_hex missing on {len(no_fp)} signals — audit record corrupt?",
            )
        )
    return problems


# ── 4. on_bar trigger timeliness ──────────────────────────────────────────────


async def eval_on_bar_trigger(*, config: dict | None = None) -> list[dict]:
    """Check that on_bar fired within lag_budget seconds after the last 1H bar close.

    Catches the 'bar收盘后on_bar未触发' bug (the original oprim crash scenario).
    Logic:
      - Compute last expected 1H bar close (floor to hour boundary in UTC)
      - Query paper.signals for any record with ts >= that boundary
      - If none AND (now - last_bar_close) > lag_budget → alert
    """
    cfg = config or {}
    lag_budget: float = cfg.get("lag_budget_seconds", 5 * 60)  # 5 min after bar close

    now = datetime.now(timezone.utc)
    last_bar_close = now.replace(minute=0, second=0, microsecond=0)
    seconds_since_close = (now - last_bar_close).total_seconds()

    # Only evaluate after the lag window has passed
    if seconds_since_close < lag_budget:
        return []

    try:
        conn = await asyncpg.connect(DB_DSN)
        row = await conn.fetchrow(
            "SELECT COUNT(*) AS cnt FROM paper.signals WHERE ts >= $1",
            last_bar_close,
        )
        await conn.close()
    except Exception as e:
        return [_alert("on_bar_trigger", "high", f"DB error checking on_bar: {e}")]

    if row["cnt"] == 0:
        return [
            _alert(
                "on_bar_trigger",
                "high",
                f"on_bar not triggered since bar close at {last_bar_close.strftime('%H:%M')} UTC "
                f"({seconds_since_close / 60:.0f}min ago, budget={lag_budget / 60:.0f}min) "
                f"— NautilusTrader bar aggregation stalled?",
            )
        ]
    return []


# ── helpers ───────────────────────────────────────────────────────────────────


def _alert(entity_id: str, severity: str, message: str) -> dict:
    return {"entity_id": entity_id, "severity": severity, "message": message}


# ── dead-man's-switch heartbeat ────────────────────────────────────────────────


async def eval_deadman_heartbeat(*, config: dict | None = None) -> list[dict]:
    """Ping an external uptime service every cycle (no-op unless HELIVEX_DEADMAN_URL
    is set, e.g. a healthchecks.io ping URL). If the monitor itself dies, or the
    host/network goes down, the pings stop and the EXTERNAL service alerts — the one
    failure the in-process Telegram alerter can never catch. Always returns healthy."""
    cfg = config or {}
    url = cfg.get("url") or os.environ.get("HELIVEX_DEADMAN_URL", "")
    if not url:
        return []
    timeout = cfg.get("timeout", 5.0)
    loop = asyncio.get_event_loop()

    def _ping():
        import urllib.request

        try:
            urllib.request.urlopen(url, timeout=timeout).read()
        except Exception as exc:  # never let a heartbeat failure break the loop
            log.warning("deadman ping failed: %s", exc)

    await loop.run_in_executor(None, _ping)
    return []


# ── 5. gateway liveness ───────────────────────────────────────────────────────


async def eval_gateway_alive(*, config: dict | None = None) -> list[dict]:
    """HTTP health check against FastAPI gateway :8765."""
    cfg = config or {}
    port: int = cfg.get("port", 8765)
    timeout: float = cfg.get("timeout", 5.0)

    loop = asyncio.get_event_loop()

    def _check() -> int | str:
        import urllib.request

        try:
            resp = urllib.request.urlopen(
                f"http://localhost:{port}/health", timeout=timeout
            )
            return resp.status
        except Exception as e:
            return str(e)

    result = await loop.run_in_executor(None, _check)
    if result == 200:
        return []
    return [
        _alert(
            "gateway_alive",
            "critical",
            f"Gateway unreachable at :{port} — {result}. systemd will restart (Restart=always).",
        )
    ]


# ── 6. frontend liveness ──────────────────────────────────────────────────────


async def eval_web_alive(*, config: dict | None = None) -> list[dict]:
    """TCP port check for Next.js frontend :3400."""
    cfg = config or {}
    port: int = cfg.get("port", 3400)
    timeout: float = cfg.get("timeout", 5.0)

    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection("127.0.0.1", port), timeout=timeout
        )
        writer.close()
        await writer.wait_closed()
        return []
    except Exception as e:
        return [
            _alert(
                "web_alive",
                "high",
                f"Frontend unreachable at :{port} — {e}. systemd will restart (Restart=on-failure).",
            )
        ]


# ── 7. L2 orderbook data flow ─────────────────────────────────────────────────


async def eval_l2_recorder_flow(*, config: dict | None = None) -> list[dict]:
    """L2 orderbook feature flow liveness via row recency.

    market_data.orderbook_features is now populated by the iris/md adapter
    (helivex-orderbook-md-adapter.timer, every 30s), not the retired
    helivex-l2recorder — that recorder is stopped and must stay stopped (it wrote
    OKX DEMO data into this same table; re-enabling it would mix DEMO into LIVE).
    Stale > stale_seconds → the adapter timer likely stalled.
    """
    cfg = config or {}
    stale_s: float = cfg.get(
        "stale_seconds", 5 * 60
    )  # 5 min (10x the 30s adapter cadence)

    try:
        conn = await asyncpg.connect(DB_DSN)
        row = await conn.fetchrow(
            "SELECT MAX(ts) AS last_ts FROM market_data.orderbook_features"
        )
        await conn.close()
    except Exception as e:
        return [_alert("l2_recorder", "high", f"DB error checking L2 flow: {e}")]

    if row is None or row["last_ts"] is None:
        return [
            _alert(
                "l2_recorder",
                "high",
                "market_data.orderbook_features empty — recorder never wrote a row",
            )
        ]

    age = (datetime.now(timezone.utc) - row["last_ts"]).total_seconds()
    if age > stale_s:
        # Auto-remediate by re-running the md adapter, not the retired recorder.
        # Rate-limited so it can't loop.
        restarted = (
            _maybe_restart("helivex-orderbook-md-adapter")
            if cfg.get("auto_restart", True)
            else False
        )
        suffix = " — auto-restarting md adapter" if restarted else ""
        return [
            _alert(
                "l2_recorder",
                "critical",
                f"No L2 row in {age / 60:.1f}min (threshold {stale_s / 60:.0f}min) "
                f"— orderbook-md-adapter stalled?{suffix}",
            )
        ]
    return []


# ── 8. DB write freshness (silent persistence-death breaker) ───────────────────


async def eval_write_freshness(*, config: dict | None = None) -> list[dict]:
    """Catch the SILENT persistence-death failure: node process ALIVE (PID up,
    ticks flowing) but no DB write landing — signals/fills frozen.

    The scalp_5m strategies fire a signal on EVERY 5-min bar (incl. NEUTRAL), so
    paper.signals must advance at least every ~5 min while the node runs. If the
    node PID is alive but the newest signal is older than stale_seconds, the write
    path has died (asyncpg pool lost its connection) — exactly the 11h gap on
    2026-06-30, which eval_ws_tick_flow's loose 70-min window let slip. Gated on
    node-alive so it never double-fires with eval_node_alive when the node is down.
    """
    cfg = config or {}
    stale_s: float = cfg.get("stale_seconds", 15 * 60)  # 3× the 5-min scalp cadence
    pid_file = Path(cfg.get("pid_file", str(PAPER_PID_FILE)))

    # The DB freshness check below IS the test, so we must NEVER silently
    # disable it just because a PID file is absent (containerised deploy: the
    # node's PID isn't visible here). Only skip when we can POSITIVELY confirm
    # the process is dead — then eval_node_alive owns the alert and we avoid a
    # duplicate. Missing/unreadable PID file → fall through and check writes.
    if pid_file.exists():
        try:
            pid = int(pid_file.read_text().strip())
            os.kill(pid, 0)
        except PermissionError:
            pass  # exists under another uid → alive
        except ProcessLookupError:
            return []  # process confirmed gone → eval_node_alive's job
        except (ValueError, OSError):
            pass  # unreadable PID → still check write freshness

    try:
        conn = await asyncpg.connect(DB_DSN)
        row = await conn.fetchrow("SELECT MAX(ts) AS last_ts FROM paper.signals")
        await conn.close()
    except Exception as e:
        return [
            _alert("write_freshness", "high", f"DB error checking write freshness: {e}")
        ]

    if row is None or row["last_ts"] is None:
        return [
            _alert(
                "write_freshness",
                "critical",
                "node ALIVE but paper.signals empty — DB persistence never started",
            )
        ]

    age = (datetime.now(timezone.utc) - row["last_ts"]).total_seconds()
    if age > stale_s:
        return [
            _alert(
                "write_freshness",
                "critical",
                f"node ALIVE but no signal persisted in {age / 60:.0f}min "
                f"(threshold {stale_s / 60:.0f}min) — DB write path dead (pool lost?), "
                f"trading is running BLIND",
            )
        ]
    return []


# ── 8b. market-data ingestion freshness ───────────────────────────────────────


async def eval_ingestion_freshness(*, config: dict | None = None) -> list[dict]:
    """Alert when the OHLCV ingestion pipeline has stopped writing.

    This is the exact gap that let market_data.* silently go EMPTY after the
    2026-06-30 host rebuild with nothing noticing: the live node feeds off the
    OKX WS and never touches these tables, so no other breaker watches them.
    We check MAX(created_at) — the last time ANY row was ingested — across the
    OHLCV tables. The 1H/5M refresh timers run hourly, so a healthy pipeline
    writes at least once an hour; > max_age_hours since the last write means the
    ingest timers are dead/uninstalled. Severity 'high' (research data — does not
    halt live trading, but must not rot unseen)."""
    cfg = config or {}
    max_age_h: float = cfg.get("max_age_hours", 3.0)
    tables = cfg.get("tables", ["market_data.ohlcv_1h", "market_data.ohlcv_5m"])

    try:
        conn = await asyncpg.connect(DB_DSN)
        try:
            out: list[dict] = []
            for tbl in tables:
                reg = await conn.fetchval("SELECT to_regclass($1)", tbl)
                if reg is None:
                    out.append(
                        _alert(
                            "ingestion_freshness",
                            "high",
                            f"{tbl} does not exist — ingestion never provisioned",
                        )
                    )
                    continue
                last = await conn.fetchval(f"SELECT MAX(created_at) FROM {tbl}")
                if last is None:
                    out.append(
                        _alert(
                            "ingestion_freshness",
                            "high",
                            f"{tbl} is EMPTY — ingestion pipeline not running",
                        )
                    )
                    continue
                age_h = (datetime.now(timezone.utc) - last).total_seconds() / 3600
                if age_h > max_age_h:
                    out.append(
                        _alert(
                            "ingestion_freshness",
                            "high",
                            f"{tbl} last written {age_h:.1f}h ago "
                            f"(threshold {max_age_h:.0f}h) — ingest timer dead?",
                        )
                    )
            return out
        finally:
            await conn.close()
    except Exception as e:
        return [
            _alert("ingestion_freshness", "high", f"DB error checking ingestion: {e}")
        ]


# ── 8c. fidelity snapshot (persistence, not an alert) ─────────────────────────


async def eval_persist_fidelity(*, config: dict | None = None) -> list[dict]:
    """Snapshot execution fidelity into paper.fidelity_summary each cycle.

    Not a health check — it returns no alerts. It piggybacks on the monitor's
    120s loop to give the orphaned fidelity_summary table a writer (audit: it had
    none), so slippage/latency/fill-rate drift is recorded over time. Failures are
    swallowed to a single 'low' alert so a persistence hiccup never breaks the
    health loop."""
    try:
        conn = await asyncpg.connect(DB_DSN)
        try:
            from paper.db import write_fidelity_summary

            await write_fidelity_summary(conn)
        finally:
            await conn.close()
        return []
    except Exception as e:
        return [_alert("fidelity_persist", "low", f"fidelity snapshot failed: {e}")]


# ── 9. backup freshness ───────────────────────────────────────────────────────


async def eval_backup_freshness(*, config: dict | None = None) -> list[dict]:
    """Alert if the newest pg_dump is older than max_age_hours — catches a missed
    or failing nightly backup (e.g. host off overnight, or pg_dump erroring). The
    cron-without-catch-up version silently skipped runs; this makes it visible."""
    cfg = config or {}
    backup_dir = Path(cfg.get("backup_dir", str(Path.home() / "backups/helivex/pg")))
    max_age_h: float = cfg.get("max_age_hours", 26)

    dumps = (
        sorted(
            backup_dir.glob("helivex_*.dump"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        if backup_dir.exists()
        else []
    )
    if not dumps:
        return [_alert("backup_freshness", "high", f"no pg_dump found in {backup_dir}")]

    age_h = (time.time() - dumps[0].stat().st_mtime) / 3600
    if age_h > max_age_h:
        return [
            _alert(
                "backup_freshness",
                "high",
                f"newest pg_dump is {age_h:.1f}h old (threshold {max_age_h:.0f}h) "
                f"— nightly backup missed or failing",
            )
        ]
    return []


# ── 10. 3O ensemble pipeline freshness (helixa→helivex 替换 P9 watchdog 等价) ──


async def eval_ensemble_freshness(*, config: dict | None = None) -> list[dict]:
    """Alert when a 3O ensemble adapter (regime / signal-engines / consensus) has
    stopped writing. Each runs on a 15-30min timer, so a healthy pipeline writes
    within `max_age_minutes`. Severity 'high' — observe-only research pipeline,
    does not halt live paper trading, but must not rot unseen (same posture as
    eval_ingestion_freshness). Replaces helixa watchdog's signal_log_alive /
    attribution_daemon_alive / hmm_regime_ttl checks."""
    cfg = config or {}
    max_age_m: float = cfg.get("max_age_minutes", 45.0)
    tables = cfg.get(
        "tables",
        ["paper.regime_state", "paper.engine_signals", "paper.consensus_signals"],
    )
    try:
        conn = await asyncpg.connect(DB_DSN)
        try:
            out: list[dict] = []
            for tbl in tables:
                reg = await conn.fetchval("SELECT to_regclass($1)", tbl)
                if reg is None:
                    continue  # not provisioned yet — not an alert (may be pre-P2/P4/P5)
                last = await conn.fetchval(f"SELECT MAX(cycle_ts) FROM {tbl}")
                if last is None:
                    continue
                age_m = (datetime.now(timezone.utc) - last).total_seconds() / 60
                if age_m > max_age_m:
                    out.append(
                        _alert(
                            "ensemble_freshness",
                            "high",
                            f"{tbl} last write {age_m:.0f}min ago (threshold "
                            f"{max_age_m:.0f}min) — 3O ensemble adapter stalled?",
                        )
                    )
            return out
        finally:
            await conn.close()
    except Exception as e:
        return [_alert("ensemble_freshness", "high", f"ensemble freshness check failed: {e}")]


# ── 11. regime-switch event notifier (helixa tg-notifier regime-switch 等价) ────

_last_regime: dict[str, str] = {}


async def eval_regime_switch(*, config: dict | None = None) -> list[dict]:
    """Emit an INFO event when an instrument's market regime changes vs. the last
    observed state (crisis/trend/range). Mirrors helixa tg-notifier's
    regime-switch push. Not a fault — an event notification through the same
    alert channels; severity 'low'. First observation seeds silently."""
    try:
        conn = await asyncpg.connect(DB_DSN)
        try:
            reg = await conn.fetchval("SELECT to_regclass('paper.regime_state')")
            if reg is None:
                return []
            rows = await conn.fetch(
                """SELECT DISTINCT ON (instrument) instrument, state
                   FROM paper.regime_state ORDER BY instrument, cycle_ts DESC"""
            )
        finally:
            await conn.close()
    except Exception as e:
        return [_alert("regime_switch", "low", f"regime switch check failed: {e}")]

    out: list[dict] = []
    for r in rows:
        inst, state = r["instrument"], r["state"]
        prev = _last_regime.get(inst)
        if prev is not None and prev != state:
            out.append(
                _alert(
                    "regime_switch",
                    "low",
                    f"{inst} regime {prev} → {state} (advisory)",
                )
            )
        _last_regime[inst] = state
    return out
