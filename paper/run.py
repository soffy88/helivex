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
import sys
from pathlib import Path

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
        print("[ABORT] OKX_LIVE=1 is set. paper/run.py must only run against OKX DEMO.", file=sys.stderr)
        sys.exit(1)
    for key in ("OKX_API_KEY", "OKX_API_SECRET", "OKX_PASSPHRASE"):
        if not os.environ.get(key):
            print(f"[ABORT] Missing required env var: {key}", file=sys.stderr)
            sys.exit(1)
    print("[paper/run.py] Safety gate passed — OKX DEMO mode.")


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


def main() -> None:
    _load_env()
    _safety_gate()
    asyncio.run(_init_db_schema())

    from paper.node import build_node
    node = build_node()
    node.build()

    try:
        print("[paper/run.py] Starting node — 3 strategies on OKX DEMO.")
        node.run()
    finally:
        node.dispose()
        print("[paper/run.py] Node stopped.")


if __name__ == "__main__":
    main()
