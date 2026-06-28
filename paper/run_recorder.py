"""paper/run_recorder.py — launch the standalone L2 order-book recorder node.

A data-only NautilusTrader node (no trading) that captures OKX L2 microstructure
features into market_data.orderbook_features. See paper/orderbook_recorder.py.

Usage:
    cd /home/soffy/projects/helivex && source venv/bin/activate
    python paper/run_recorder.py

Requires OKX DEMO creds + OKX_WS_PROXY in .env (same as the paper node).
"""
from __future__ import annotations

import os
import signal
import sys
from pathlib import Path

_PID_FILE = Path("/tmp/helivex_l2recorder.pid")
_ROOT = Path(__file__).parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


def _load_env() -> None:
    env_path = _ROOT / ".env"
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
        print("[ABORT] OKX_LIVE=1 — recorder must run against OKX DEMO only.", file=sys.stderr)
        sys.exit(1)
    for key in ("OKX_API_KEY", "OKX_API_SECRET", "OKX_PASSPHRASE"):
        if not os.environ.get(key):
            print(f"[ABORT] Missing required env var: {key}", file=sys.stderr)
            sys.exit(1)


def main() -> None:
    _load_env()
    _safety_gate()

    _PID_FILE.write_text(str(os.getpid()))
    signal.signal(signal.SIGTERM, lambda *_: (_PID_FILE.unlink(missing_ok=True), sys.exit(0)))

    from paper.orderbook_recorder import build_recorder_node
    node = build_recorder_node()
    node.build()
    try:
        print("[run_recorder] L2 recorder node starting — OKX DEMO, BTC/ETH/SOL books.")
        node.run()
    finally:
        _PID_FILE.unlink(missing_ok=True)
        node.dispose()
        print("[run_recorder] recorder stopped.")


if __name__ == "__main__":
    main()
