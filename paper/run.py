"""paper/run.py — Launch helivex paper trading node against OKX Demo.

Usage:
    cd /home/soffy/projects/helivex
    source venv/bin/activate
    python paper/run.py

Required env vars (set in .env or export):
    OKX_API_KEY, OKX_API_SECRET, OKX_PASSPHRASE  — OKX DEMO credentials
    HELIVEX_AUDIT_PRIVATE_KEY_B64                 — Ed25519 signing key (optional; STANDARD tier if missing)
    HELIVEX_AUDIT_PUBLIC_KEY_B64                  — Ed25519 verify key  (optional)

Gate: refuses to start if OKX_LIVE is set to '1' (guard against live key accident).
"""

from __future__ import annotations

import asyncio
import os
import signal
import sys
from pathlib import Path

_PID_FILE = Path("/tmp/helivex_paper_node.pid")

# Ensure helivex root is on sys.path regardless of invocation method.
_ROOT = Path(__file__).parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


def _load_env() -> None:
    env_path = Path(__file__).parent.parent / ".env"
    if env_path.exists():
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip())


def _safety_gate() -> None:
    if os.environ.get("OKX_LIVE", "") == "1":
        print(
            "[ABORT] OKX_LIVE=1 is set. paper/run.py must only run against OKX DEMO.",
            file=sys.stderr,
        )
        sys.exit(1)
    for key in ("OKX_API_KEY", "OKX_API_SECRET", "OKX_PASSPHRASE"):
        if not os.environ.get(key):
            print(f"[ABORT] Missing required env var: {key}", file=sys.stderr)
            sys.exit(1)
    print("[paper/run.py] Safety gate passed — OKX DEMO mode.")


def _strategy_governance() -> list[dict]:
    """Cross-reference each strategy config against the gate ledger (.gate_trials.json).

    Returns one row per strategies/*.yaml: {name, config, verdict}, where verdict
    is the latest recorded gate outcome (PASS/FAIL), 'NO-GO' if the YAML pins
    gate_status: no-go, or 'UNVALIDATED' if the strategy was never gated. This is
    the governance signal the audit flagged as missing — trading was happening
    with zero visibility into whether anything had passed a gate.
    """
    import json

    import yaml

    root = Path(__file__).parent.parent
    trials_path = root / ".gate_trials.json"
    latest: dict[str, str] = {}
    if trials_path.exists():
        try:
            data = json.loads(trials_path.read_text())
            for t in data.get("history", []):
                cfg = os.path.basename(str(t.get("config", "")))
                if cfg:
                    latest[cfg] = t.get(
                        "verdict", "?"
                    )  # history is ordered → last wins
        except (ValueError, OSError):
            pass

    rows: list[dict] = []
    for yml in sorted((root / "strategies").glob("*.yaml")):
        name = yml.stem
        try:
            cfg = yaml.safe_load(yml.read_text()) or {}
        except (ValueError, OSError):
            cfg = {}
        pinned = str(cfg.get("gate_status", "")).lower()
        if pinned in ("no-go", "nogo"):
            verdict = "NO-GO"
        else:
            verdict = latest.get(yml.name, "UNVALIDATED")
        rows.append({"name": name, "config": yml.name, "verdict": verdict})
    return rows


def _governance_gate() -> None:
    """Print the governance report; in LIVE mode, refuse any non-PASS strategy.

    Paper mode (OKX DEMO) intentionally observes FAILED / NO-GO / UNVALIDATED
    strategies for execution-fidelity measurement, so it only WARNS. Real money
    (OKX_LIVE=1) must never trade an ungated edge: a non-PASS strategy aborts the
    run unless HELIVEX_LIVE_OVERRIDE names it explicitly (break-glass, audited).
    """
    rows = _strategy_governance()
    live = os.environ.get("OKX_LIVE", "") == "1"
    override = set(
        s.strip()
        for s in os.environ.get("HELIVEX_LIVE_OVERRIDE", "").split(",")
        if s.strip()
    )
    print("[paper/run.py] ── Strategy governance (gate ledger) ─────────────")
    for r in rows:
        mark = "✓" if r["verdict"] == "PASS" else "✗"
        print(f"    {mark} {r['name']:<16} {r['config']:<22} verdict={r['verdict']}")

    unvalidated = [
        r for r in rows if r["verdict"] != "PASS" and r["name"] not in override
    ]
    if live and unvalidated:
        names = ", ".join(r["name"] for r in unvalidated)
        print(
            f"[ABORT] OKX_LIVE=1 but these strategies have no PASS verdict: {names}. "
            f"A gate PASS (or HELIVEX_LIVE_OVERRIDE) is required to trade real money.",
            file=sys.stderr,
        )
        sys.exit(1)
    if unvalidated:
        names = ", ".join(r["name"] for r in unvalidated)
        print(
            f"[paper/run.py] ⚠ paper-observing UNVALIDATED/FAILED strategies: {names} "
            f"(fidelity holdout only — NOT promotable to live without a gate PASS)."
        )


def _wait_okx_reachable(timeout_s: float = 600.0) -> None:
    """Block until OKX REST answers through the configured proxy (OKX_WS_PROXY).

    2026-07-10 incident: after a host reboot this container came up seconds
    before the sing-box tunnel. NT's OKX adapter does NOT retry its initial
    instrument load — the first connect got "connection reset", the node timed
    out, then sat RUNNING with both engines disconnected for 24h (zero bars,
    zero fills, zero logs). Any HTTP response (even a 4xx — Cloudflare blocks
    urllib's TLS fingerprint) proves the tunnel is passing traffic; only
    transport-level errors mean "not ready yet".
    """
    import time
    import urllib.error
    import urllib.request

    proxy = os.environ.get("OKX_WS_PROXY", "")
    handlers = (
        [urllib.request.ProxyHandler({"http": proxy, "https": proxy})] if proxy else []
    )
    opener = urllib.request.build_opener(*handlers)
    url = "https://www.okx.com/api/v5/public/time"
    deadline = time.monotonic() + timeout_s
    delay = 2.0
    while True:
        try:
            opener.open(url, timeout=10).close()
            print("[paper/run.py] OKX reachable via proxy — proceeding.")
            return
        except urllib.error.HTTPError:
            print(
                "[paper/run.py] Proxy tunnel up (HTTP response from OKX) — proceeding."
            )
            return
        except OSError as e:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                print(
                    f"[ABORT] OKX unreachable via proxy after {timeout_s:.0f}s: {e}",
                    file=sys.stderr,
                )
                sys.exit(1)  # non-zero exit → docker restart policy retries us
            print(f"[paper/run.py] OKX not reachable yet ({e}); retry in {delay:.0f}s…")
            time.sleep(min(delay, remaining))
            delay = min(delay * 2, 30.0)


def _start_zombie_guard(node) -> None:
    """Kill the process if both engines stay disconnected ≥5 min — docker restarts us.

    Companion to _wait_okx_reachable: the preflight guards startup, this guards
    the running node. NT keeps the TradingNode in RUNNING even when the initial
    connect timed out (or a WS session dies beyond the adapter's own reconnect),
    which supervision by exit code can never see. Dying is the only way docker's
    restart policy can heal us.
    """
    import threading
    import time

    def _watch() -> None:
        time.sleep(120.0)  # grace: normal startup / adapter reconnects
        misses = 0
        while True:
            try:
                ok = (
                    node.kernel.data_engine.check_connected()
                    and node.kernel.exec_engine.check_connected()
                )
            except Exception:
                ok = False
            misses = 0 if ok else misses + 1
            if misses >= 5:
                print(
                    "[paper/run.py] FATAL: engines disconnected ≥5 min — exiting so "
                    "docker restarts the node.",
                    file=sys.stderr,
                )
                sys.stderr.flush()
                _remove_pid()
                os._exit(1)
            time.sleep(60.0)

    threading.Thread(target=_watch, name="zombie-guard", daemon=True).start()


async def _init_db_schema() -> None:
    import asyncpg
    from paper.db import DB_DSN, ensure_schema

    try:
        conn = await asyncpg.connect(DB_DSN)
        await ensure_schema(conn)
        await conn.close()
        print("[paper/run.py] DB schema ensured.")
    except Exception as e:
        print(f"[paper/run.py] DB init warning (non-fatal): {e}")


def _write_pid() -> None:
    _PID_FILE.write_text(str(os.getpid()))
    print(f"[paper/run.py] PID {os.getpid()} → {_PID_FILE}")


def _remove_pid() -> None:
    try:
        _PID_FILE.unlink(missing_ok=True)
    except OSError:
        pass


def main() -> None:
    # Line-buffer stdout so run.py's diagnostics (safety gate, governance report,
    # PID, startup) reach `docker logs` immediately. Piped stdout is block-buffered
    # by default, which hid these behind NautilusTrader's own (unbuffered) logger.
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:
        pass
    _load_env()
    _safety_gate()
    _governance_gate()
    _wait_okx_reachable()
    asyncio.run(_init_db_schema())

    _write_pid()
    # Clean up PID on SIGTERM so monitor detects intentional shutdown immediately
    signal.signal(signal.SIGTERM, lambda *_: (_remove_pid(), sys.exit(0)))

    from paper.node import build_node

    node = build_node()
    node.build()
    _start_zombie_guard(node)

    try:
        print(
            "[paper/run.py] Starting node — 4 strategies on OKX DEMO (scalp_5m is NO-GO observation)."
        )
        node.run()
    finally:
        _remove_pid()
        node.dispose()
        print("[paper/run.py] Node stopped.")


if __name__ == "__main__":
    main()
