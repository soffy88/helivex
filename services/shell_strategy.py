"""helivex thin-shell strategy: NautilusTrader → 3O omodul (unidirectional).

Dependency direction:
  nautilus_trader → [helivex] → 3O (obase / oprim / oskill / omodul / oservi)
  3O NEVER imports nautilus_trader (enforced by tests/test_import_paths.py)
"""
from __future__ import annotations

import numpy as np

from omodul._base import compute_fingerprint
from omodul.strategies import bocpd_trend_following

from nautilus_trader.config import StrategyConfig
from nautilus_trader.model.data import Bar
from nautilus_trader.model.enums import OrderSide
from nautilus_trader.trading.strategy import Strategy

_BOCPD_CONFIG = {
    "bocpd_hazard": 0.01,
    "trend_window": 24,
    "confidence_threshold": 0.60,
    "direction_mode": "long_only",
    "target_annual_vol": 0.20,
    "max_position_pct": 0.10,
    "max_gross_leverage": 1.0,
    "rebalance_threshold": 0.02,
    "daily_loss_halt_pct": 0.05,
    "weekly_loss_halt_pct": 0.10,
    "volatility_halt_multiplier": 3.0,
    "baseline_realized_vol": 0.60,
    "daily_volume_usd": 5e9,
    "realized_vol_30d": 0.60,
    "n_twap_slices": 1,
    "slice_duration_sec": 0,
}

_MIN_BARS = 30  # need enough history for BOCPD


class ShellConfig(StrategyConfig, frozen=True):
    instrument_id: str
    bar_type: str
    capital_usd: float = 100_000.0


class ShellStrategy(Strategy):
    """Calls omodul.bocpd_trend_following on each bar; trades on strong trend signal."""

    def __init__(self, config: ShellConfig) -> None:
        super().__init__(config)
        self._closes: list[float] = []
        cap = config.capital_usd
        self._equity: list[float] = [cap, cap]  # needs >=2 points for risk gate
        self._last_fingerprint: str = ""
        self._position_open: bool = False

    def on_start(self) -> None:
        from nautilus_trader.model.data import BarType
        self._bar_type = BarType.from_str(self.config.bar_type)
        self._instrument_id_obj = self.cache.instrument(
            __import__("nautilus_trader.model.identifiers", fromlist=["InstrumentId"])
            .InstrumentId.from_str(self.config.instrument_id)
        )
        self.subscribe_bars(self._bar_type)

    def on_bar(self, bar: Bar) -> None:
        close = float(bar.close)
        self._closes.append(close)

        # Audit fingerprint (D2 evidence)
        self._last_fingerprint = compute_fingerprint({
            "ts": bar.ts_event, "c": str(bar.close), "v": str(bar.volume)
        })

        if len(self._closes) < _MIN_BARS:
            return

        returns = np.diff(np.log(self._closes[-(_MIN_BARS + 1):])).tolist()
        sym = self.config.instrument_id

        market_state = {
            "symbols": [sym],
            "features": {f"returns_{sym}": returns},
            "current_positions": {},
            "capital_usd": self.config.capital_usd,
            "equity_curve": self._equity[-30:],
        }

        decision = bocpd_trend_following(market_state, _BOCPD_CONFIG)
        signal = decision["signals"].get(sym, {})
        direction = signal.get("direction", "neutral")
        strength = float(signal.get("strength", 0.0))

        instrument = self.cache.instrument(
            __import__("nautilus_trader.model.identifiers", fromlist=["InstrumentId"])
            .InstrumentId.from_str(self.config.instrument_id)
        )
        if instrument is None:
            return

        if direction == "long" and strength > 0.1 and not self._position_open:
            qty = instrument.make_qty(0.001)
            order = self.order_factory.market(
                instrument_id=instrument.id,
                order_side=OrderSide.BUY,
                quantity=qty,
            )
            self.submit_order(order)
            self._position_open = True

        elif direction == "neutral" and self._position_open:
            self.close_all_positions(instrument.id)
            self._position_open = False

    def on_stop(self) -> None:
        from nautilus_trader.model.identifiers import InstrumentId
        inst_id = InstrumentId.from_str(self.config.instrument_id)
        self.close_all_positions(inst_id)
        self.log.info(f"last_omodul_fingerprint={self._last_fingerprint}")
