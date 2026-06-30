#!/usr/bin/env python3
"""Re-run the SWAP YAML gates (trials #1-6 territory) with vs without perp funding
carry, to show the integrity-corrected verdict — WITHOUT appending to the
append-only .gate_trials.json (the gate ledger has a rising multiple-testing bar;
silently adding trials would mutate the research record). Read-only.

Trials #7-13 are bespoke ops/scripts/*_gate.py with their own P&L and are not
covered here (funding lives in tools/strategy_gate._signals_to_pnl).

  python ops/scripts/funding_rerun_compare.py
"""
from __future__ import annotations

import asyncio
import importlib

import numpy as np

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))

from strategy_gate import (  # type: ignore
    STRATEGY_MAP, _dsr_threshold, _fetch_funding, _fetch_ohlcv, _funding_into_bars,
    _funding_symbol, _periods_per_year, _resample_ohlcv, _resample_to_1d, _sharpe,
    _signals_to_pnl, _walk_forward_gate,
)
import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONFIGS = ["strategies/trend_dual.yaml", "strategies/vwap_mr_1h.yaml", "strategies/spot_trend_1d.yaml"]


async def _eval(cfg, inst, ppy, gate_cfg, funding_into):
    """One gate evaluation for a given funding_into (None = funding off)."""
    raw = await _fetch_ohlcv(inst, cfg.get("db_source", "okx_swap_1h"))
    if cfg.get("resample_to_1d"):
        ohlcv = _resample_to_1d(raw)
    elif cfg.get("resample_bars", 1) > 1:
        ohlcv = _resample_ohlcv(raw, int(cfg["resample_bars"]))
    elif cfg.get("resample_from_1h", 1) > 1:
        ohlcv = _resample_ohlcv(raw, int(cfg["resample_from_1h"]))
    else:
        ohlcv = {k: raw[k] for k in ("ts", "open", "high", "low", "close", "volume")}

    mod_path, fn_name = STRATEGY_MAP[cfg["strategy"]]
    strategy_fn = getattr(importlib.import_module(mod_path), fn_name)
    res = strategy_fn({"ohlcv": ohlcv, "instrument": inst,
                       "current_positions": {}, "capital_usd": 10000.0}, cfg)
    signals, cost_bps = res["signals"], res["cost_bps"]
    direction = cfg.get("signal_logic", {}).get("direction", "both")

    fi = None
    if funding_into is not None:
        fi = _funding_into_bars(ohlcv["ts"], funding_into)
    pnl = _signals_to_pnl(signals, ohlcv["close"], cost_bps, direction=direction, funding_into=fi)
    gate = _walk_forward_gate(pnl, int(gate_cfg.get("n_splits", 6)),
                              int(gate_cfg.get("embargo_bars", 50)), ppy,
                              float(gate_cfg.get("pbo_threshold", 0.5)))
    return _sharpe(pnl, ppy), gate, ohlcv["ts"]


async def main() -> None:
    print(f"{'config / instrument':<46}{'gross SR':>20}{'mean_oos':>20}{'verdict':>12}")
    print(f"{'':<46}{'off → on':>20}{'off → on':>20}")
    print("-" * 98)
    for cpath in CONFIGS:
        cfg = yaml.safe_load(open(PROJECT_ROOT / cpath))
        ppy = _periods_per_year(cfg.get("timeframe", "1H"))
        gate_cfg = cfg.get("gate", {})
        for inst in cfg.get("instruments", []):
            try:
                sr_off, g_off, _ = await _eval(cfg, inst, ppy, gate_cfg, None)
                frows = await _fetch_funding(inst) if inst.upper().endswith("SWAP") else None
                if frows:
                    sr_on, g_on, _ = await _eval(cfg, inst, ppy, gate_cfg, frows)
                    nfund = len(frows)
                else:
                    sr_on, g_on, nfund = sr_off, g_off, 0
            except Exception as e:
                print(f"{cpath} {inst:<20} SKIP: {e}")
                continue
            label = f"{cpath.split('/')[-1]:<22}{inst}"
            print(f"{label:<46}"
                  f"{sr_off:>8.3f} → {sr_on:<8.3f}"
                  f"{g_off['mean_oos_sharpe']:>9.3f} → {g_on['mean_oos_sharpe']:<8.3f}"
                  f"{g_off['status']:>6}→{g_on['status']:<5}  (fund n={nfund})")


if __name__ == "__main__":
    asyncio.run(main())
