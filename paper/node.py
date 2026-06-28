"""paper.node — NautilusTrader TradingNode wired to OKX Demo for all 4 strategies.

Strategies:
  1. Donchian4H   — BTC/ETH/SOL USDT-SWAP 4H trend (long + short)
  2. VwapMR1H     — SOL USDT-SWAP 1H VWAP mean reversion (taker)
  3. SpotTrend1D  — BTC/ETH spot daily Donchian trend (long-only)
  4. Scalp5M      — BTC/ETH/SOL USDT-SWAP 5M VWAP-MR scalper ⚠ NO-GO observation only
                    R5: gross +1.33 Sharpe but taker costs 307%/yr kill it.
                    Paper run measures real fill rate/slippage vs backtest assumptions.

All use OKXEnvironment.DEMO. Real OKX keys are only read from env; never hardcoded.
"""
from __future__ import annotations

import os
from pathlib import Path

import yaml

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

from paper.strategies.donchian_4h   import Donchian4H,   Donchian4HConfig
from paper.strategies.vwap_mr_1h    import VwapMR1H,    VwapMR1HConfig
from paper.strategies.spot_trend_1d import SpotTrend1D, SpotTrend1DConfig
from paper.strategies.scalp_5m      import Scalp5M,     Scalp5MConfig


def _okx_env():
    from nautilus_trader.adapters.okx.config import OKXEnvironment
    return OKXEnvironment.DEMO


def _okx_instrument_type_swap():
    from nautilus_trader.adapters.okx.config import OKXInstrumentType
    return OKXInstrumentType.SWAP


def _okx_instrument_type_spot():
    from nautilus_trader.adapters.okx.config import OKXInstrumentType
    return OKXInstrumentType.SPOT


_STRAT_DIR = Path(__file__).parent.parent / "strategies"


def _live(yaml_name: str) -> dict:
    """Read the `live` param block from a strategy YAML (editable via the Configure
    tab). Returns {} on any error so the hardcoded defaults below still apply."""
    try:
        cfg = yaml.safe_load((_STRAT_DIR / yaml_name).read_text()) or {}
        return cfg.get("live") or {}
    except Exception:
        return {}


def build_node() -> TradingNode:
    api_key     = os.environ["OKX_API_KEY"]
    api_secret  = os.environ["OKX_API_SECRET"]
    passphrase  = os.environ["OKX_PASSPHRASE"]
    # NT's Rust WS client bypasses shell proxy env vars; wire it explicitly.
    proxy_url   = os.environ.get("OKX_WS_PROXY") or None

    # Single unified client for both SWAP and SPOT — eliminates venue routing
    # overwrite bug where two OKX clients both register venue=OKX and the second
    # (OKX_SPOT) silently shadows the first, routing all bar subscriptions wrong.
    okx_data = OKXDataClientConfig(
        api_key=api_key,
        api_secret=api_secret,
        api_passphrase=passphrase,
        environment=_okx_env(),
        instrument_types=(_okx_instrument_type_swap(), _okx_instrument_type_spot()),
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

    # live params from YAML (editable via Configure tab) — defaults = prior hardcoded
    td = _live("trend_dual.yaml")
    vw = _live("vwap_mr_1h.yaml")
    sp = _live("spot_trend_1d.yaml")

    # Strategy 1 — Donchian 4H SWAP (BTC, ETH, SOL)
    # LAST-INTERNAL: NT aggregates from trade ticks — no dependency on business WS candle push
    donchian_btc = Donchian4HConfig(
        instrument_id="BTC-USDT-SWAP.OKX",
        bar_type="BTC-USDT-SWAP.OKX-4-HOUR-LAST-INTERNAL",
        n_enter=int(td.get("n_enter", 20)), n_exit=int(td.get("n_exit", 10)), qty_usd=float(td.get("qty_usd", 200.0)),
    )
    donchian_eth = Donchian4HConfig(
        instrument_id="ETH-USDT-SWAP.OKX",
        bar_type="ETH-USDT-SWAP.OKX-4-HOUR-LAST-INTERNAL",
        n_enter=int(td.get("n_enter", 20)), n_exit=int(td.get("n_exit", 10)), qty_usd=float(td.get("qty_usd", 200.0)),
    )
    donchian_sol = Donchian4HConfig(
        instrument_id="SOL-USDT-SWAP.OKX",
        bar_type="SOL-USDT-SWAP.OKX-4-HOUR-LAST-INTERNAL",
        n_enter=int(td.get("n_enter", 20)), n_exit=int(td.get("n_exit", 10)), qty_usd=float(td.get("qty_usd", 200.0)),
    )

    # Strategy 2 — VWAP-MR 1H SWAP (SOL)
    vwap_sol = VwapMR1HConfig(
        instrument_id="SOL-USDT-SWAP.OKX",
        bar_type="SOL-USDT-SWAP.OKX-1-HOUR-LAST-INTERNAL",
        vwap_n=int(vw.get("vwap_n", 4)), z_thr=float(vw.get("z_thr", 2.0)),
        hold=int(vw.get("hold", 6)), qty_usd=float(vw.get("qty_usd", 200.0)),
    )

    # Strategy 3 — Daily Donchian Spot (BTC, ETH)
    spot_btc = SpotTrend1DConfig(
        instrument_id="BTC-USDT.OKX",
        bar_type="BTC-USDT.OKX-1-DAY-LAST-INTERNAL",
        n_enter=int(sp.get("n_enter", 20)), n_exit=int(sp.get("n_exit", 10)),
        bear_ma=int(sp.get("bear_ma", 200)), qty_usd=float(sp.get("qty_usd", 200.0)),
    )
    spot_eth = SpotTrend1DConfig(
        instrument_id="ETH-USDT.OKX",
        bar_type="ETH-USDT.OKX-1-DAY-LAST-INTERNAL",
        n_enter=int(sp.get("n_enter", 20)), n_exit=int(sp.get("n_exit", 10)),
        bear_ma=int(sp.get("bear_ma", 200)), qty_usd=float(sp.get("qty_usd", 200.0)),
    )

    node_config = TradingNodeConfig(
        trader_id="HELIVEX-PAPER-001",
        exec_engine=LiveExecEngineConfig(reconciliation=True),
        data_clients={
            "OKX": okx_data,
        },
        exec_clients={
            "OKX": okx_exec,
        },
        # strategies added via node.add_strategy() below — TradingNodeConfig.strategies
        # expects ImportableStrategyConfig in v1.228+, not StrategyConfig instances
    )

    node = TradingNode(config=node_config)
    node.add_data_client_factory("OKX", OKXLiveDataClientFactory)
    node.add_exec_client_factory("OKX", OKXLiveExecClientFactory)

    # Strategy 4 — Scalp 5M VWAP-MR SWAP (BTC, ETH, SOL) ⚠ NO-GO observation
    # R5: gross +1.33 Sharpe but taker costs 307%/yr kill net return.
    # Paper purpose: measure real fill rate/slippage vs R5 backtest assumptions.
    sc = _live("scalp_5m.yaml")
    scalp_btc = Scalp5MConfig(
        instrument_id="BTC-USDT-SWAP.OKX",
        bar_type="BTC-USDT-SWAP.OKX-5-MINUTE-LAST-INTERNAL",
        vwap_n=int(sc.get("vwap_n", 12)), z_thr=float(sc.get("z_thr", 2.0)),
        hold=int(sc.get("hold", 6)), qty_usd=float(sc.get("qty_usd", 50.0)),
    )
    scalp_eth = Scalp5MConfig(
        instrument_id="ETH-USDT-SWAP.OKX",
        bar_type="ETH-USDT-SWAP.OKX-5-MINUTE-LAST-INTERNAL",
        vwap_n=int(sc.get("vwap_n", 12)), z_thr=float(sc.get("z_thr", 2.0)),
        hold=int(sc.get("hold", 6)), qty_usd=float(sc.get("qty_usd", 50.0)),
    )
    scalp_sol = Scalp5MConfig(
        instrument_id="SOL-USDT-SWAP.OKX",
        bar_type="SOL-USDT-SWAP.OKX-5-MINUTE-LAST-INTERNAL",
        vwap_n=int(sc.get("vwap_n", 12)), z_thr=float(sc.get("z_thr", 2.0)),
        hold=int(sc.get("hold", 6)), qty_usd=float(sc.get("qty_usd", 50.0)),
    )

    node.trader.add_strategy(Donchian4H(donchian_btc))
    node.trader.add_strategy(Donchian4H(donchian_eth))
    node.trader.add_strategy(Donchian4H(donchian_sol))
    node.trader.add_strategy(VwapMR1H(vwap_sol))
    node.trader.add_strategy(SpotTrend1D(spot_btc))
    node.trader.add_strategy(SpotTrend1D(spot_eth))
    node.trader.add_strategy(Scalp5M(scalp_btc))
    node.trader.add_strategy(Scalp5M(scalp_eth))
    node.trader.add_strategy(Scalp5M(scalp_sol))

    return node
