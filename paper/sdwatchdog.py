"""paper.sdwatchdog — systemd watchdog keep-alive tied to NautilusTrader liveness.

A tiny NT Actor pings systemd's WATCHDOG=1 on a clock timer. The timer only fires
while the engine's event loop is actually processing, so a wedged/deadlocked node
stops pinging and systemd (WatchdogSec=) restarts it — the one failure mode the
data-staleness monitor catches slowly and exit-code supervision misses entirely.

No Type=notify needed: WatchdogSec works with Type=simple. Everything is a no-op
when NOTIFY_SOCKET is unset (running outside systemd), so local runs are unaffected.
"""
from __future__ import annotations

import os
import socket
from datetime import timedelta

from nautilus_trader.common.actor import Actor
from nautilus_trader.common.config import ActorConfig


def sd_notify(msg: bytes) -> bool:
    """Send a datagram to systemd's notify socket. False if not running under it."""
    addr = os.environ.get("NOTIFY_SOCKET")
    if not addr:
        return False
    path = ("\0" + addr[1:]) if addr.startswith("@") else addr
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as s:
            s.connect(path)
            s.sendall(msg)
        return True
    except OSError:
        return False


class WatchdogConfig(ActorConfig, frozen=True):
    interval_s: float = 30.0   # ping cadence; set WatchdogSec >= ~2x this


class WatchdogActor(Actor):
    """Pings WATCHDOG=1 on a repeating timer; READY=1 once at start (harmless under
    Type=simple). If the engine loop wedges, pings stop and systemd restarts."""

    def __init__(self, config: WatchdogConfig) -> None:
        super().__init__(config)
        self._n = 0

    def on_start(self) -> None:
        sd_notify(b"READY=1")
        self.clock.set_timer(
            "sd_watchdog",
            timedelta(seconds=self.config.interval_s),
            callback=self._ping,
        )
        self.log.info(f"[watchdog] sd_notify keep-alive every {self.config.interval_s}s")

    def _ping(self, _event) -> None:
        self._n += 1
        ok = sd_notify(b"WATCHDOG=1")
        if self._n <= 2:
            self.log.info(f"[watchdog] WATCHDOG=1 sent={ok}")


def attach_watchdog(node, interval_s: float = 30.0) -> None:
    """Add the watchdog actor to a built TradingNode (before node.build())."""
    node.trader.add_actor(WatchdogActor(WatchdogConfig(interval_s=interval_s)))
