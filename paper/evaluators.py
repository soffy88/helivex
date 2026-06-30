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
    if a restart was issued. Used ONLY for the data-only L2 recorder — the trading
    node is left alert-only so a human decides on a disruptive bounce.
    """
    stamp = Path(f"/tmp/helivex_restart_{service}.stamp")
    try:
        if stamp.exists() and (time.time() - stamp.stat().st_mtime) < cooldown_s:
            return False
        stamp.write_text(str(time.time()))
        subprocess.run(["systemctl", "--user", "restart", service],
                       check=False, timeout=30,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        log.warning("auto-remediation: restarted %s", service)
        return True
    except Exception as exc:
        log.error("auto-remediation failed for %s: %s", service, exc)
        return False


# ── 1. process liveness ───────────────────────────────────────────────────────

async def eval_node_alive(*, config: dict | None = None) -> list[dict]:
    """Check that paper/run.py PID is still alive."""
    cfg = config or {}
    pid_file = Path(cfg.get("pid_file", str(PAPER_PID_FILE)))

    if not pid_file.exists():
        return [_alert(
            "paper_node_pid", "critical",
            f"PID file missing ({pid_file}) — node not started or crashed",
        )]

    try:
        pid = int(pid_file.read_text().strip())
    except (ValueError, OSError) as e:
        return [_alert("paper_node_pid", "high", f"Cannot read PID file: {e}")]

    try:
        os.kill(pid, 0)
        return []  # alive
    except ProcessLookupError:
        return [_alert(
            "paper_node_pid", "critical",
            f"Process PID={pid} not found — node has died",
        )]
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
        return [_alert(
            "okx_ws_tick", "high",
            "paper.signals is empty — node never connected to OKX WS or DB unavailable",
        )]

    age = (datetime.now(timezone.utc) - row["last_ts"]).total_seconds()
    if age > stale_s:
        return [_alert(
            "okx_ws_tick", "critical",
            f"No OKX signal in {age / 60:.0f}min (threshold {stale_s / 60:.0f}min) "
            f"— OKX WS disconnected?",
        )]
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
    no_fp    = [r["strategy_id"] for r in rows if not r["fingerprint_hex"]]

    problems = []
    if unsigned:
        problems.append(
            _alert("audit_chain", "high",
                   f"GOLD tier but sig_b64 empty on {len(unsigned)} recent signals "
                   f"({unsigned[:3]}) — Ed25519 signing degraded to STANDARD?")
        )
    if no_fp:
        problems.append(
            _alert("audit_chain", "high",
                   f"fingerprint_hex missing on {len(no_fp)} signals — audit record corrupt?")
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
        return [_alert(
            "on_bar_trigger", "high",
            f"on_bar not triggered since bar close at {last_bar_close.strftime('%H:%M')} UTC "
            f"({seconds_since_close / 60:.0f}min ago, budget={lag_budget / 60:.0f}min) "
            f"— NautilusTrader bar aggregation stalled?",
        )]
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
            resp = urllib.request.urlopen(f"http://localhost:{port}/health", timeout=timeout)
            return resp.status
        except Exception as e:
            return str(e)

    result = await loop.run_in_executor(None, _check)
    if result == 200:
        return []
    return [_alert(
        "gateway_alive", "critical",
        f"Gateway unreachable at :{port} — {result}. systemd will restart (Restart=always).",
    )]


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
        return [_alert(
            "web_alive", "high",
            f"Frontend unreachable at :{port} — {e}. systemd will restart (Restart=on-failure).",
        )]


# ── 7. L2 recorder data flow ──────────────────────────────────────────────────

async def eval_l2_recorder_flow(*, config: dict | None = None) -> list[dict]:
    """L2 recorder liveness via row recency — catches a SILENT WS stall (process
    stays up, data stops), which Restart=always cannot detect.

    Recorder persists every ~10s × 3 instruments; stale > stale_seconds → WS dead.
    """
    cfg = config or {}
    stale_s: float = cfg.get("stale_seconds", 5 * 60)  # 5 min (30× the 10s cadence)

    try:
        conn = await asyncpg.connect(DB_DSN)
        row = await conn.fetchrow(
            "SELECT MAX(ts) AS last_ts FROM market_data.orderbook_features")
        await conn.close()
    except Exception as e:
        return [_alert("l2_recorder", "high", f"DB error checking L2 flow: {e}")]

    if row is None or row["last_ts"] is None:
        return [_alert("l2_recorder", "high",
                       "market_data.orderbook_features empty — recorder never wrote a row")]

    age = (datetime.now(timezone.utc) - row["last_ts"]).total_seconds()
    if age > stale_s:
        # Auto-remediate: the recorder is data-only, so a bounce is safe. The
        # resilient DB pool (paper.db_pool) fixes the common cause, but this catches
        # any other silent stall. Rate-limited so it can't loop.
        restarted = _maybe_restart("helivex-l2recorder") if cfg.get("auto_restart", True) else False
        suffix = " — auto-restarting recorder" if restarted else ""
        return [_alert("l2_recorder", "critical",
                       f"No L2 row in {age / 60:.1f}min (threshold {stale_s / 60:.0f}min) "
                       f"— recorder WS stalled?{suffix}")]
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

    # Only meaningful while the node is actually running; a dead node is
    # eval_node_alive's job.
    if not pid_file.exists():
        return []
    try:
        pid = int(pid_file.read_text().strip())
        os.kill(pid, 0)
    except PermissionError:
        pass  # exists under another uid → alive
    except (ValueError, OSError):
        return []  # unreadable PID / process gone → not our concern

    try:
        conn = await asyncpg.connect(DB_DSN)
        row = await conn.fetchrow("SELECT MAX(ts) AS last_ts FROM paper.signals")
        await conn.close()
    except Exception as e:
        return [_alert("write_freshness", "high", f"DB error checking write freshness: {e}")]

    if row is None or row["last_ts"] is None:
        return [_alert("write_freshness", "critical",
                       "node ALIVE but paper.signals empty — DB persistence never started")]

    age = (datetime.now(timezone.utc) - row["last_ts"]).total_seconds()
    if age > stale_s:
        return [_alert("write_freshness", "critical",
                       f"node ALIVE but no signal persisted in {age / 60:.0f}min "
                       f"(threshold {stale_s / 60:.0f}min) — DB write path dead (pool lost?), "
                       f"trading is running BLIND")]
    return []


# ── 9. backup freshness ───────────────────────────────────────────────────────

async def eval_backup_freshness(*, config: dict | None = None) -> list[dict]:
    """Alert if the newest pg_dump is older than max_age_hours — catches a missed
    or failing nightly backup (e.g. host off overnight, or pg_dump erroring). The
    cron-without-catch-up version silently skipped runs; this makes it visible."""
    cfg = config or {}
    backup_dir = Path(cfg.get("backup_dir", str(Path.home() / "backups/helivex/pg")))
    max_age_h: float = cfg.get("max_age_hours", 26)

    dumps = (
        sorted(backup_dir.glob("helivex_*.dump"), key=lambda p: p.stat().st_mtime, reverse=True)
        if backup_dir.exists() else []
    )
    if not dumps:
        return [_alert("backup_freshness", "high", f"no pg_dump found in {backup_dir}")]

    age_h = (time.time() - dumps[0].stat().st_mtime) / 3600
    if age_h > max_age_h:
        return [_alert("backup_freshness", "high",
                       f"newest pg_dump is {age_h:.1f}h old (threshold {max_age_h:.0f}h) "
                       f"— nightly backup missed or failing")]
    return []
