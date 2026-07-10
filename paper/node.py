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

from paper.strategies.donchian_4h import Donchian4H, Donchian4HConfig
from paper.strategies.vwap_mr_1h import VwapMR1H, VwapMR1HConfig
from paper.strategies.spot_trend_1d import SpotTrend1D, SpotTrend1DConfig
from paper.strategies.scalp_5m import Scalp5M, Scalp5MConfig
from paper.strategies.trend_follower_port import (
    TrendFollowerPort,
    TrendFollowerPortConfig,
)
from paper.strategies.scalper_v2_port import (
    ScalperV2Port,
    ScalperV2PortConfig,
)
from paper.strategies.futures_signal_port import (
    FuturesSignalPort,
    FuturesSignalPortConfig,
)


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
    api_key = os.environ["OKX_API_KEY"]
    api_secret = os.environ["OKX_API_SECRET"]
    passphrase = os.environ["OKX_PASSPHRASE"]
    # NT's Rust WS client bypasses shell proxy env vars; wire it explicitly.
    proxy_url = os.environ.get("OKX_WS_PROXY") or None

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
        # SWAP + SPOT: exec was SWAP-only, which silently left SpotTrend1D
        # (BTC/ETH-USDT spot) with NO execution path — signals fired but orders
        # could never route/fill (0 fills ever). Data client already loads both.
        instrument_types=(_okx_instrument_type_swap(), _okx_instrument_type_spot()),
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
        n_enter=int(td.get("n_enter", 20)),
        n_exit=int(td.get("n_exit", 10)),
        qty_usd=float(td.get("qty_usd", 200.0)),
    )
    donchian_eth = Donchian4HConfig(
        instrument_id="ETH-USDT-SWAP.OKX",
        bar_type="ETH-USDT-SWAP.OKX-4-HOUR-LAST-INTERNAL",
        n_enter=int(td.get("n_enter", 20)),
        n_exit=int(td.get("n_exit", 10)),
        qty_usd=float(td.get("qty_usd", 200.0)),
    )
    donchian_sol = Donchian4HConfig(
        instrument_id="SOL-USDT-SWAP.OKX",
        bar_type="SOL-USDT-SWAP.OKX-4-HOUR-LAST-INTERNAL",
        n_enter=int(td.get("n_enter", 20)),
        n_exit=int(td.get("n_exit", 10)),
        qty_usd=float(td.get("qty_usd", 200.0)),
    )

    # Strategy 2 — VWAP-MR 1H SWAP (SOL)
    vwap_sol = VwapMR1HConfig(
        instrument_id="SOL-USDT-SWAP.OKX",
        bar_type="SOL-USDT-SWAP.OKX-1-HOUR-LAST-INTERNAL",
        vwap_n=int(vw.get("vwap_n", 4)),
        z_thr=float(vw.get("z_thr", 2.0)),
        hold=int(vw.get("hold", 6)),
        qty_usd=float(vw.get("qty_usd", 200.0)),
    )

    # Strategy 3 — Daily Donchian Spot (BTC, ETH)
    spot_btc = SpotTrend1DConfig(
        instrument_id="BTC-USDT.OKX",
        bar_type="BTC-USDT.OKX-1-DAY-LAST-INTERNAL",
        n_enter=int(sp.get("n_enter", 20)),
        n_exit=int(sp.get("n_exit", 10)),
        bear_ma=int(sp.get("bear_ma", 200)),
        qty_usd=float(sp.get("qty_usd", 200.0)),
    )
    spot_eth = SpotTrend1DConfig(
        instrument_id="ETH-USDT.OKX",
        bar_type="ETH-USDT.OKX-1-DAY-LAST-INTERNAL",
        n_enter=int(sp.get("n_enter", 20)),
        n_exit=int(sp.get("n_exit", 10)),
        bear_ma=int(sp.get("bear_ma", 200)),
        qty_usd=float(sp.get("qty_usd", 200.0)),
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
        vwap_n=int(sc.get("vwap_n", 12)),
        z_thr=float(sc.get("z_thr", 2.0)),
        hold=int(sc.get("hold", 6)),
        qty_usd=float(sc.get("qty_usd", 50.0)),
    )
    scalp_eth = Scalp5MConfig(
        instrument_id="ETH-USDT-SWAP.OKX",
        bar_type="ETH-USDT-SWAP.OKX-5-MINUTE-LAST-INTERNAL",
        vwap_n=int(sc.get("vwap_n", 12)),
        z_thr=float(sc.get("z_thr", 2.0)),
        hold=int(sc.get("hold", 6)),
        qty_usd=float(sc.get("qty_usd", 50.0)),
    )
    scalp_sol = Scalp5MConfig(
        instrument_id="SOL-USDT-SWAP.OKX",
        bar_type="SOL-USDT-SWAP.OKX-5-MINUTE-LAST-INTERNAL",
        vwap_n=int(sc.get("vwap_n", 12)),
        z_thr=float(sc.get("z_thr", 2.0)),
        hold=int(sc.get("hold", 6)),
        qty_usd=float(sc.get("qty_usd", 50.0)),
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

    # Strategy 5 (ported) — helixa trend_follower (Donchian20 + ADX gate + Chandelier
    # ATR×3 trailing + 30d time stop). OBSERVE ONLY: trade_enabled=False → logs signals
    # for gating, submits NO orders until it passes DSR/PBO gate + a human flips it.
    tf = _live("trend_follower_port.yaml")
    _tf_enabled = bool(tf.get("trade_enabled", False))
    for _sym in ("BTC", "ETH", "SOL"):
        node.trader.add_strategy(
            TrendFollowerPort(
                TrendFollowerPortConfig(
                    instrument_id=f"{_sym}-USDT-SWAP.OKX",
                    bar_type=f"{_sym}-USDT-SWAP.OKX-1-DAY-LAST-INTERNAL",
                    donchian_period=int(tf.get("donchian_period", 20)),
                    adx_period=int(tf.get("adx_period", 14)),
                    adx_entry=float(tf.get("adx_entry", 20.0)),
                    adx_exit=float(tf.get("adx_exit", 15.0)),
                    chandelier_period=int(tf.get("chandelier_period", 22)),
                    chandelier_mult=float(tf.get("chandelier_mult", 3.0)),
                    max_holding_days=int(tf.get("max_holding_days", 30)),
                    qty_usd=float(tf.get("qty_usd", 200.0)),
                    use_ema=bool(tf.get("use_ema", True)),
                    ema_period=int(tf.get("ema_period", 50)),
                    use_macd=bool(tf.get("use_macd", True)),
                    macd_fast=int(tf.get("macd_fast", 12)),
                    macd_slow=int(tf.get("macd_slow", 26)),
                    macd_signal=int(tf.get("macd_signal", 9)),
                    trade_enabled=_tf_enabled,
                )
            )
        )

    # Strategy 6 (ported) — helixa intraday_scalper_v2 (5m dual-mode ADX-hysteresis:
    # RSI/BB mean-reversion ↔ BB-breakout + ATR×1.5 trailing + 4h time stop). OBSERVE
    # ONLY: trade_enabled=False → logs signals for gating, submits NO orders.
    sv = _live("scalper_v2_port.yaml")
    _sv_enabled = bool(sv.get("trade_enabled", False))
    for _sym in ("BTC", "ETH", "SOL"):
        node.trader.add_strategy(
            ScalperV2Port(
                ScalperV2PortConfig(
                    instrument_id=f"{_sym}-USDT-SWAP.OKX",
                    bar_type=f"{_sym}-USDT-SWAP.OKX-5-MINUTE-LAST-INTERNAL",
                    bb_period=int(sv.get("bb_period", 20)),
                    bb_k=float(sv.get("bb_k", 2.0)),
                    rsi_period=int(sv.get("rsi_period", 14)),
                    adx_period=int(sv.get("adx_period", 14)),
                    adx_enter_breakout=float(sv.get("adx_enter_breakout", 22.0)),
                    adx_exit_breakout=float(sv.get("adx_exit_breakout", 18.0)),
                    cooldown_bars=int(sv.get("cooldown_bars", 4)),
                    trailing_atr_mult=float(sv.get("trailing_atr_mult", 1.5)),
                    breakout_exit_adx=float(sv.get("breakout_exit_adx", 15.0)),
                    max_holding_bars=int(sv.get("max_holding_bars", 48)),
                    qty_usd=float(sv.get("qty_usd", 50.0)),
                    use_ema=bool(sv.get("use_ema", True)),
                    ema_period=int(sv.get("ema_period", 50)),
                    use_macd=bool(sv.get("use_macd", True)),
                    macd_fast=int(sv.get("macd_fast", 12)),
                    macd_slow=int(sv.get("macd_slow", 26)),
                    macd_signal=int(sv.get("macd_signal", 9)),
                    trade_enabled=_sv_enabled,
                )
            )
        )

    # Strategy 7 (ported) — helixa futures-signal-engine (1H breakout + volume surge +
    # RSI band). OBSERVE ONLY: trade_enabled=False → logs signals, submits NO orders.
    fs = _live("futures_signal_port.yaml")
    _fs_enabled = bool(fs.get("trade_enabled", False))
    for _sym in ("BTC", "ETH", "SOL"):
        node.trader.add_strategy(
            FuturesSignalPort(
                FuturesSignalPortConfig(
                    instrument_id=f"{_sym}-USDT-SWAP.OKX",
                    bar_type=f"{_sym}-USDT-SWAP.OKX-1-HOUR-LAST-INTERNAL",
                    breakout_period=int(fs.get("breakout_period", 20)),
                    vol_ma_period=int(fs.get("vol_ma_period", 20)),
                    vol_surge_mult=float(fs.get("vol_surge_mult", 1.5)),
                    rsi_period=int(fs.get("rsi_period", 14)),
                    qty_usd=float(fs.get("qty_usd", 100.0)),
                    use_ema=bool(fs.get("use_ema", True)),
                    ema_period=int(fs.get("ema_period", 50)),
                    use_macd=bool(fs.get("use_macd", True)),
                    macd_fast=int(fs.get("macd_fast", 12)),
                    macd_slow=int(fs.get("macd_slow", 26)),
                    macd_signal=int(fs.get("macd_signal", 9)),
                    trade_enabled=_fs_enabled,
                )
            )
        )

    from paper.sdwatchdog import attach_watchdog

    attach_watchdog(node)  # systemd WatchdogSec keep-alive (no-op outside systemd)

    return node
