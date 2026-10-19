"""运行两个 continuous_counterfactual 案例（研究态）。

案例 1 — vwap_mr_1h：锚定「seed0 best 参数 + 6bps」的 test 事实，
   推演成本 +2bp / 波动 +30% 的 L3 反事实，并与引擎重跑交叉验证。
案例 2 — scalper_v2_port（1H 变体）：SCM 推演「净正所需成本门槛」（breakeven），
   并用 continuous_plan_with_hybrid_bo 在 SCM 上做联合干预规划。

输出：audit/counterfactual_*.jsonl + 控制台表。
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # repo 根（helivex 包）

import numpy as np

from helivex_adapter.backtest_bridge import run_backtest
from helivex_adapter.counterfactual import (
    _window_vol,
    breakeven_cost,
    build_scm,
    counterfactual_curve,
    plan_max_sharpe,
    sample_dataset,
    save_jsonl,
)
from helivex_adapter.discovery_types import BacktestRequest

PQ = {s: f"data/scan_{s.split('-')[0].lower()}_1h.parquet" for s in
      ["BTC-USDT-SWAP", "ETH-USDT-SWAP", "SOL-USDT-SWAP"]}
WINDOWS = [("2026-04-02", "2026-05-15"), ("2026-05-15", "2026-07-01")]


def _engine_check(sid, params, cost, sym, win):
    m = run_backtest(BacktestRequest(
        strategy_id=sid, params=params, symbols=[sym],
        start=win[0], end=win[1], timeframe="1h",
        extra={"parquet_path": PQ[sym], "cpcv": dict(n_groups=6, n_test_groups=1,
                                                     embargo_bars=4),
               "cost_model": dict(taker_fee_bps=cost, fixed_slippage_bps=0.0)}))
    return float(m.sharpe), float(m.total_return), float(m.max_drawdown)


def case_vwap_mr() -> None:
    params = {"vwap_n": 6.56, "std_n": 43.75, "z_thr": 2.02, "hold": 7.18,
              "sl_std": 1.63, "min_rr": 2.39, "cooldown_after_sl": 5.45}
    grid = {"z_thr": [1.5, 2.0, 2.5, 3.0], "hold": [4.0, 7.0, 10.0],
            "sl_std": [1.0, 1.63, 2.0]}
    syms = ["BTC-USDT-SWAP", "ETH-USDT-SWAP", "SOL-USDT-SWAP"]
    t0 = time.time()
    # 全交叉：4z × 3hold × 3sl × 4cost × 2win = 288 次组合回测（成本效应不被参数混杂）
    data = sample_dataset(strategy_id="vwap_mr_1h", timeframe="1h",
                          parquet_map=PQ, windows=WINDOWS, param_grid=grid,
                          cost_grid=[2.0, 4.0, 6.0, 8.0], seed=0,
                          symbols=syms, full_cross=True)
    print(f"[vwap_mr] 采样 {len(data['sharpe'])} 次组合回测（全交叉）, {time.time()-t0:.0f}s")
    scm = build_scm(data, list(grid.keys()))
    # 事实：组合 + 窗口2（05-15→07-01）+ seed0 参数 + 6bps
    vol_fact = float(np.mean([_window_vol(s, "1h", *WINDOWS[1], PQ[s]) for s in syms]))
    vol_alt = float(np.mean([_window_vol(s, "1h", *WINDOWS[0], PQ[s]) for s in syms]))
    factual = {"vol": np.asarray([vol_fact]), "cost_bps": np.asarray([6.0]),
               "z_thr": np.asarray([params["z_thr"]]),
               "hold": np.asarray([params["hold"]]),
               "sl_std": np.asarray([params["sl_std"]])}
    # L3 推演：成本冲击（clamp 到合理范围）
    curve = counterfactual_curve(scm, factual, "sharpe", "cost_bps",
                                 [2.0, 4.0, 6.0, 8.0, 10.0], clamp=(-3.0, 3.0))
    print("成本→Sharpe L3 推演（组合口径）:")
    for r in curve:
        print(f"  do(cost={r['value']:>4.1f}) → sharpe {r['l3']:+.3f}")
    # 交叉验证：引擎组合重跑 cost=2 / 8（推演 vs 真相）
    xval = []
    for cost in (2.0, 8.0):
        m = run_backtest(BacktestRequest(
            strategy_id="vwap_mr_1h", params=params, symbols=syms,
            start=WINDOWS[1][0], end=WINDOWS[1][1], timeframe="1h",
            extra={"parquet_path": {s: PQ[s] for s in syms},
                   "cpcv": dict(n_groups=6, n_test_groups=1, embargo_bars=4),
                   "cost_model": dict(taker_fee_bps=cost, fixed_slippage_bps=0.0)}))
        l3 = next(r["l3"] for r in curve if abs(r["value"] - cost) < 1e-9)
        xval.append({"cost": cost, "engine_sharpe": float(m.sharpe), "scm_l3": l3})
        print(f"  交叉验证 cost={cost}: 引擎 {m.sharpe:+.3f} vs SCM L3 {l3:+.3f}")
    # 波动压力：do(vol = 另一窗口实测值)（训练分布内，非外推）
    mdd_now = scm.l3_counterfactual(factual, {"cost_bps": [6.0]}, "mdd")
    mdd_up = scm.l3_counterfactual(factual, {"cost_bps": [6.0], "vol": [vol_alt]}, "mdd")
    sr_up = scm.l3_counterfactual(factual, {"cost_bps": [6.0], "vol": [vol_alt]}, "sharpe")
    print(f"波动压力(分布内): 事实 vol={vol_fact:.3f} → MDD {float(mdd_now[0]):.3f}; "
          f"do(vol={vol_alt:.3f}) → MDD {float(mdd_up[0]):.3f}, sharpe {float(sr_up[0]):+.3f}")
    save_jsonl("integrations/veya_loop_quant/audit/counterfactual_vwap_mr.jsonl", [
        {"case": "vwap_mr_1h", "kind": "l3_cost_curve", "factual": params,
         "curve": curve},
        {"case": "vwap_mr_1h", "kind": "engine_crossval", "checks": xval},
        {"case": "vwap_mr_1h", "kind": "vol_stress",
         "factual_vol": vol_fact, "stressed_vol": vol_alt,
         "mdd_factual": float(mdd_now[0]), "mdd_stressed": float(mdd_up[0]),
         "sharpe_stressed": float(sr_up[0])},
    ])


def case_scalper() -> None:
    params = {"bb_k": 2.0, "adx_enter_breakout": 22.0, "trailing_atr_mult": 1.5,
              "rsi_oversold": 30.0, "sl_atr_mult": 1.0, "min_rr": 1.5}
    syms = ["BTC-USDT-SWAP", "ETH-USDT-SWAP", "SOL-USDT-SWAP"]
    t0 = time.time()
    data = sample_dataset(strategy_id="scalper_v2_port", timeframe="1h",
                          parquet_map=PQ, windows=WINDOWS,
                          param_grid={"bb_k": [1.5, 2.0, 2.5],
                                      "sl_atr_mult": [0.8, 1.2]},
                          cost_grid=[2.0, 4.0, 6.0, 8.0, 10.0],
                          seed=1, symbols=syms, full_cross=True)
    print(f"[scalper] 采样 {len(data['sharpe'])} 次组合回测（全交叉）, {time.time()-t0:.0f}s")
    scm = build_scm(data, ["bb_k", "sl_atr_mult"])
    vol_fact = float(np.mean([_window_vol(s, "1h", *WINDOWS[1], PQ[s]) for s in syms]))
    factual = {"vol": np.asarray([vol_fact]), "cost_bps": np.asarray([6.0]),
               "bb_k": np.asarray([2.0]), "sl_atr_mult": np.asarray([1.0])}
    be = breakeven_cost(scm, factual)
    print(f"scalper 净正成本门槛（L3 扫点）: cost={be:.2f} bps" if be
          else "scalper: 未找到 sharpe 过零点（全区间为负）")
    # 联合干预规划：在 SCM 上 BO 找 (cost, bb_k) 使 sharpe 最大
    plan = plan_max_sharpe(scm, factual,
                           {"cost_bps": (1.0, 12.0), "bb_k": (1.5, 2.8)})
    print(f"continuous_plan_with_hybrid_bo: best = { {k: round(v, 2) for k, v in plan['best_intervention'].items()} }, "
          f"sharpe = {plan['best_value']:+.3f}, 调用 {plan['n_calls']} 次")
    # 引擎交叉验证 plan 的干预点
    m = run_backtest(BacktestRequest(
        strategy_id="scalper_v2_port", params=params, symbols=syms,
        start=WINDOWS[1][0], end=WINDOWS[1][1], timeframe="1h",
        extra={"parquet_path": {s: PQ[s] for s in syms},
               "cpcv": dict(n_groups=6, n_test_groups=1, embargo_bars=4),
               "cost_model": dict(taker_fee_bps=plan["best_intervention"]["cost_bps"],
                                   fixed_slippage_bps=0.0)}))
    print(f"  引擎重跑干预点: sharpe {m.sharpe:+.3f} (SCM 预测 {plan['best_value']:+.3f})")
    save_jsonl("integrations/veya_loop_quant/audit/counterfactual_scalper.jsonl", [
        {"case": "scalper_v2_1h", "kind": "breakeven_cost", "value": be},
        {"case": "scalper_v2_1h", "kind": "plan",
         "best_intervention": plan["best_intervention"],
         "scm_best_sharpe": plan["best_value"], "n_calls": plan["n_calls"],
         "engine_sharpe_at_plan": float(m.sharpe)},
    ])


if __name__ == "__main__":
    case_vwap_mr()
    case_scalper()
