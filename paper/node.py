"""paper.node — NautilusTrader TradingNode wired to OKX Demo for all 3 strategies.

Strategies:
  1. Donchian4H   — BTC/ETH/SOL USDT-SWAP 4H trend (long + short)
  2. VwapMR1H     — SOL USDT-SWAP 1H VWAP mean reversion (taker)
  3. SpotTrend1D  — BTC/ETH spot daily Donchian trend (long-only)

All use OKXEnvironment.DEMO. Real OKX keys are only read from env; never hardcoded.
"""
from __future__ import annotations

import os

from nautilus_trader.adapters.okx.config import (
    OKXDataClientConfig,
    OKXExecClientConfig,
)
from nautilus_trader.adapters.okx.factories import (
    OKXLiveDataClientFactory,
    OKXLiveExecClientFactory,
)
from nautilus_trader.config import (
    InstrumentProviderConfig,
    LiveExecEngineConfig,
    TradingNodeConfig,
)
from nautilus_trader.live.node import TradingNode

from paper.strategies.donchian_4h import Donchian4H, Donchian4HConfig
from paper.strategies.vwap_mr_1h  import VwapMR1H,  VwapMR1HConfig
from paper.strategies.spot_trend_1d import SpotTrend1D, SpotTrend1DConfig


def _okx_env():
    from nautilus_trader.adapters.okx.config import OKXEnvironment
    return OKXEnvironment.DEMO


def _okx_instrument_type_swap():
    from nautilus_trader.adapters.okx.config import OKXInstrumentType
    return OKXInstrumentType.SWAP


def _okx_instrument_type_spot():
    from nautilus_trader.adapters.okx.config import OKXInstrumentType
    return OKXInstrumentType.SPOT


def build_node() -> TradingNode:
    api_key     = os.environ["OKX_API_KEY"]
    api_secret  = os.environ["OKX_API_SECRET"]
    passphrase  = os.environ["OKX_PASSPHRASE"]
    # NT's Rust WS client bypasses shell proxy env vars; wire it explicitly.
    proxy_url   = os.environ.get("OKX_WS_PROXY") or None

    okx_data_swap = OKXDataClientConfig(
        api_key=api_key,
        api_secret=api_secret,
        api_passphrase=passphrase,
        environment=_okx_env(),
        instrument_types=(_okx_instrument_type_swap(),),
        proxy_url=proxy_url,
        instrument_provider=InstrumentProviderConfig(load_all=True),
    )
    okx_data_spot = OKXDataClientConfig(
        api_key=api_key,
        api_secret=api_secret,
        api_passphrase=passphrase,
        environment=_okx_env(),
        instrument_types=(_okx_instrument_type_spot(),),
        proxy_url=proxy_url,
        instrument_provider=InstrumentProviderConfig(load_all=True),
    )
    okx_exec = OKXExecClientConfig(
        api_key=api_key,
        api_secret=api_secret,
        api_passphrase=passphrase,
        environment=_okx_env(),
        instrument_types=(_okx_instrument_type_swap(),),
        proxy_url=proxy_url,
        instrument_provider=InstrumentProviderConfig(load_all=True),
    )

    # Strategy 1 — Donchian 4H SWAP (BTC, ETH, SOL)
    donchian_btc = Donchian4HConfig(
        instrument_id="BTC-USDT-SWAP.OKX",
        bar_type="BTC-USDT-SWAP.OKX-4-HOUR-LAST-EXTERNAL",
        n_enter=20, n_exit=10, qty_usd=200.0,
    )
    donchian_eth = Donchian4HConfig(
        instrument_id="ETH-USDT-SWAP.OKX",
        bar_type="ETH-USDT-SWAP.OKX-4-HOUR-LAST-EXTERNAL",
        n_enter=20, n_exit=10, qty_usd=200.0,
    )
    donchian_sol = Donchian4HConfig(
        instrument_id="SOL-USDT-SWAP.OKX",
        bar_type="SOL-USDT-SWAP.OKX-4-HOUR-LAST-EXTERNAL",
        n_enter=20, n_exit=10, qty_usd=200.0,
    )

    # Strategy 2 — VWAP-MR 1H SWAP (SOL)
    vwap_sol = VwapMR1HConfig(
        instrument_id="SOL-USDT-SWAP.OKX",
        bar_type="SOL-USDT-SWAP.OKX-1-HOUR-LAST-EXTERNAL",
        vwap_n=4, z_thr=2.0, hold=6, qty_usd=200.0,
    )

    # Strategy 3 — Daily Donchian Spot (BTC, ETH)
    spot_btc = SpotTrend1DConfig(
        instrument_id="BTC-USDT.OKX",
        bar_type="BTC-USDT.OKX-1-DAY-LAST-EXTERNAL",
        n_enter=20, n_exit=10, bear_ma=200, qty_usd=200.0,
    )
    spot_eth = SpotTrend1DConfig(
        instrument_id="ETH-USDT.OKX",
        bar_type="ETH-USDT.OKX-1-DAY-LAST-EXTERNAL",
        n_enter=20, n_exit=10, bear_ma=200, qty_usd=200.0,
    )

    node_config = TradingNodeConfig(
        trader_id="HELIVEX-PAPER-001",
        exec_engine=LiveExecEngineConfig(reconciliation=True),
        data_clients={
            "OKX_SWAP": okx_data_swap,
            "OKX_SPOT": okx_data_spot,
        },
        exec_clients={
            "OKX": okx_exec,
        },
        # strategies added via node.add_strategy() below — TradingNodeConfig.strategies
        # expects ImportableStrategyConfig in v1.228+, not StrategyConfig instances
    )

    node = TradingNode(config=node_config)
    node.add_data_client_factory("OKX_SWAP", OKXLiveDataClientFactory)
    node.add_data_client_factory("OKX_SPOT", OKXLiveDataClientFactory)
    node.add_exec_client_factory("OKX", OKXLiveExecClientFactory)

    node.trader.add_strategy(Donchian4H(donchian_btc))
    node.trader.add_strategy(Donchian4H(donchian_eth))
    node.trader.add_strategy(Donchian4H(donchian_sol))
    node.trader.add_strategy(VwapMR1H(vwap_sol))
    node.trader.add_strategy(SpotTrend1D(spot_btc))
    node.trader.add_strategy(SpotTrend1D(spot_eth))

    return node
