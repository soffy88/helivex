"""paper/monitor.py — Standalone health monitor process for helivex paper trading.

Runs the AlerterEngine in a background thread so it stays alive independently
of the NautilusTrader node.  This is the second of the two background processes:

  Process 1: paper/run.py      — NautilusTrader trading node
  Process 2: paper/monitor.py  — AlerterEngine health watchdog (this file)

Usage:
  cd /home/soffy/projects/helivex
  source venv/bin/activate
  python paper/monitor.py           # foreground
  nohup python paper/monitor.py &   # background

Env vars (optional):
  TG_BOT_TOKEN  — Telegram Bot API token
  TG_CHAT_ID    — Telegram chat/user ID

Exits cleanly on SIGINT / SIGTERM.
"""
from __future__ import annotations

import logging
import os
import signal
import sys
import threading
import time
from pathlib import Path

_ROOT = Path(__file__).parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%SZ",
)
log = logging.getLogger("paper.monitor")


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


def main() -> None:
    _load_env()

    from paper.alerter import build_alerter
    engine = build_alerter()

    stop_event = threading.Event()

    def _handle_signal(sig, _frame):
        log.info("Received signal %s — stopping alerter", sig)
        engine.stop()
        stop_event.set()

    signal.signal(signal.SIGINT,  _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    monitor_thread = threading.Thread(target=engine.run, daemon=True, name="alerter")
    monitor_thread.start()
    log.info(
        "paper/monitor.py started — AlerterEngine '%s' running every %ss",
        engine.name,
        engine.trigger.get("on_interval", "?"),
    )

    try:
        while not stop_event.is_set():
            time.sleep(5)
    finally:
        engine.stop()
        monitor_thread.join(timeout=10)
        log.info("paper/monitor.py stopped cleanly")


if __name__ == "__main__":
    main()
