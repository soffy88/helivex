"""helivex thin-shell strategy: NautilusTrader calls 3O omodul — unidirectional only.

Dependency direction:
  nautilus_trader → [helivex] → 3O (obase / oprim / oskill / omodul / oservi)
  3O NEVER imports nautilus_trader (enforced by tests/test_unidirectional.py)
"""
from __future__ import annotations

from omodul._base import compute_fingerprint  # pure: sha256(canonical json)[:24]

from nautilus_trader.config import StrategyConfig
from nautilus_trader.model.data import Bar
from nautilus_trader.trading.strategy import Strategy


class ShellConfig(StrategyConfig, frozen=True):
    instrument_id: str
    bar_type: str


class ShellStrategy(Strategy):
    """Minimal helivex strategy that proves NautilusTrader → omodul call works."""

    def __init__(self, config: ShellConfig) -> None:
        super().__init__(config)
        self._last_fingerprint: str = ""

    def on_start(self) -> None:
        from nautilus_trader.model.data import BarType
        self._bar_type = BarType.from_str(self.config.bar_type)
        self.subscribe_bars(self._bar_type)

    def on_bar(self, bar: Bar) -> None:
        # Call 3O omodul pure function — no auth, no IO, no nautilus dependency
        self._last_fingerprint = compute_fingerprint({
            "open": str(bar.open),
            "high": str(bar.high),
            "low": str(bar.low),
            "close": str(bar.close),
            "volume": str(bar.volume),
            "ts": bar.ts_event,
        })

    def on_stop(self) -> None:
        self.log.info(f"last_omodul_fingerprint={self._last_fingerprint}")
