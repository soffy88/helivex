"""成本反事实：scalper_v2_port 最优参数在不同执行成本下的净 Sharpe（研究态）。

用途：回答「净正需要什么成本条件」——taker 比例 / bps 敏感性（spec §5
continuous_counterfactual 的轻量版：同一参数，干预成本假设，看指标变化）。
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # repo 根（helivex 包）

from helivex_adapter.backtest_bridge import run_backtest
from helivex_adapter.discovery_types import BacktestRequest

PARAMS = dict(rsi_oversold=30.0, sl_atr_mult=1.0, min_rr=1.5, bb_k=2.0,
              trailing_atr_mult=1.5, cooldown_after_sl=0.0)
WINDOWS = [("train", "2026-04-02", "2026-06-15"),
           ("test", "2026-06-15", "2026-08-01")]


def _run(taker_frac: float, fee_bps: float, slip_bps: float, start: str, end: str):
    req = BacktestRequest(
        strategy_id="scalper_v2_port", params=PARAMS, symbols=["BTC-USDT-SWAP"],
        start=start, end=end, timeframe="5m",
        extra={
            "parquet_path": "data/scan_btc_5m.parquet",
            "cpcv": dict(n_groups=6, n_test_groups=1, embargo_bars=12),
            "cost_model": dict(taker_fee_bps=fee_bps, maker_fee_bps=2.0,
                               assume_taker_fraction=taker_frac,
                               fixed_slippage_bps=slip_bps),
        },
    )
    m = run_backtest(req)
    return m


def main() -> None:
    rows = []
    for taker in (1.0, 0.5, 0.0):
        for fee, slip in ((5.0, 1.0), (2.0, 0.0)):
            out = {"taker_frac": taker, "taker_bps": fee, "slip_bps": slip}
            for tag, s, e in WINDOWS:
                m = _run(taker, fee, slip, s, e)
                out[tag] = {"sharpe": round(m.sharpe, 4), "ret": round(m.total_return, 4),
                            "n_trades": m.n_trades, "cost": round(m.cost_drag, 4),
                            "turn": round(m.turnover, 2)}
            rows.append(out)
            print(json.dumps(out, ensure_ascii=False))
    Path("integrations/veya_loop_quant/audit").mkdir(parents=True, exist_ok=True)
    with open("integrations/veya_loop_quant/audit/scalper_cost_counterfactual.jsonl", "a") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    t0 = time.time()
    main()
    print(f"elapsed {time.time()-t0:.1f}s", file=sys.stderr)
