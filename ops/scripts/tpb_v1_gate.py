"""TPB-001 gate — 冻结配置单次验证 + 判决(HELIVEX-IMPL_SPEC-TPB-001 §7/§8)。

验证层复用 tools/strategy_gate.py 的既有实现(_walk_forward_gate / _dsr_threshold),
使本次数字与登记表 #1–#28 严格可比 —— 这是 registry 完整性的要求,比自造一套
"更好的"指标重要。

判据口径(与仓库既有 28 个 trial 一致):
  DSR   = deflated_sharpe = mean_oos_sharpe − std_oos_sharpe(年化 Sharpe 单位,
          可为负),门槛 = _dsr_threshold(N) = 期望最大 Sharpe。§8 的
          "组合net DSR > 真实N门槛" 即 deflated_sharpe > 2.0594。
  PBO   = pbo_cscv(真 CSCV logit-rank PBO,块自助生成配置维度,单配置可算)。
          同时报告 F5 冻结的跨配置 PBO(用 T3 七配置)作为交叉印证。
  另外报告 Bailey-LdP 真 DSR 概率(n=交易笔数,spec N3),仓库标注其为
          non-gating,此处同样仅报告不判决。

用法:
    python ops/scripts/tpb_v1_gate.py            # 完整验证 + 判决
    python ops/scripts/tpb_v1_gate.py --dry      # 只跑中心配置(KILL-1 探针)
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import math
import sys
from pathlib import Path

import numpy as np

PROJ = Path(__file__).resolve().parent.parent.parent
if str(PROJ) not in sys.path:
    sys.path.insert(0, str(PROJ))

from tools.strategy_gate import (  # noqa: E402
    _deflated_sharpe_real,
    _dsr_threshold,
    _load_trials,
    _save_trial,
    _walk_forward_gate,
)
from ops.scripts.tpb_v1_runner import (  # noqa: E402
    INSTRUMENTS,
    TPBConfig,
    build_dataset,
    run_config,
)

PERIODS_1H = 24 * 365  # 8760,加密 24/7
N_SPLITS = 6  # 与全部策略 YAML 的 gate.n_splits 一致
EMBARGO_BARS = 50  # F6
PURGE_BARS = 48  # F6b:= X4 最大持仓,标签purge 必须覆盖持仓期
PBO_THRESHOLD = 0.5  # §8

# T3 扰动网格:中心 + 单参数 ±20%(§7 T3 预注册,不选优)
GRID = [
    TPBConfig(label="center"),
    TPBConfig(adx_entry=20.0, label="adx20"),
    TPBConfig(adx_entry=30.0, label="adx30"),
    TPBConfig(sl_atr=1.6, label="sl1.6"),
    TPBConfig(sl_atr=2.4, label="sl2.4"),
    TPBConfig(trail_atr=2.4, label="trail2.4"),
    TPBConfig(trail_atr=3.6, label="trail3.6"),
]

CRISIS = [
    (
        "2024-08-05",
        dt.datetime(2024, 8, 2, tzinfo=dt.timezone.utc),
        dt.datetime(2024, 8, 9, tzinfo=dt.timezone.utc),
    ),
    (
        "2025-10-10",
        dt.datetime(2025, 10, 7, tzinfo=dt.timezone.utc),
        dt.datetime(2025, 10, 14, tzinfo=dt.timezone.utc),
    ),
]


def _bar_pnl(curve: list[tuple[dt.datetime, float]]) -> np.ndarray:
    if len(curve) < 2:
        return np.zeros(0)
    return np.diff(np.asarray([e for _, e in curve], dtype=float))


def _sharpe_ann(pnl: np.ndarray, ppy: int = PERIODS_1H) -> float:
    if len(pnl) < 3:
        return 0.0
    sd = float(np.std(pnl))
    if sd < 1e-12:
        return 0.0
    return float(np.mean(pnl)) / sd * math.sqrt(ppy)


def _cross_config_pbo(results: list[dict]) -> float:
    """F5:用 T3 七配置做跨配置 PBO。切成 N_SPLITS 段,每段算各配置 IS/OOS Sharpe。

    PBO = IS-最优配置在 OOS 低于中位数的比例。内部 argmax 是统计量定义的一部分,
    不等于采用该配置(§7 T3「禁止挑最好的一组替换中心」不受影响)。
    """
    series = [_bar_pnl(r["equity_curve"]) for r in results]
    n = min(len(s) for s in series)
    if n < N_SPLITS * 2:
        return float("nan")
    series = [s[:n] for s in series]
    bounds = [round(i * n / N_SPLITS) for i in range(N_SPLITS + 1)]
    below = 0
    for i in range(N_SPLITS):
        lo, hi = bounds[i], bounds[i + 1]
        is_sr, oos_sr = [], []
        for s in series:
            oos = s[lo:hi]
            is_ = np.concatenate([s[:lo], s[hi:]])
            is_sr.append(_sharpe_ann(is_))
            oos_sr.append(_sharpe_ann(oos))
        best = int(np.argmax(is_sr))
        if oos_sr[best] < float(np.median(oos_sr)):
            below += 1
    return below / N_SPLITS


def _crisis_report(res: dict) -> list[dict]:
    out = []
    for label, lo, hi in CRISIS:
        tr = [t for t in res["trades"] if lo <= t.entry_ts < hi]
        closed = [t for t in tr if t.exit_ts]
        out.append(
            {
                "window": label,
                "n_entries": len(tr),
                "net_pnl": round(sum(t.net_pnl for t in closed), 2),
                "gross_pnl": round(sum(t.gross_pnl for t in closed), 2),
                "note": "regime门未开闸/无入场" if not tr else "有入场",
            }
        )
    return out


def _freq_report(res: dict, days: float) -> dict:
    tr = res["trades"]
    n = len(tr)
    holds = [t.bars_held for t in tr if t.bars_held]
    reasons: dict[str, int] = {}
    for t in tr:
        reasons[t.exit_reason] = reasons.get(t.exit_reason, 0) + 1
    placed = res.get("orders_placed", 0)
    return {
        "n_trades": n,
        "trades_per_day_per_asset": round(n / days / len(INSTRUMENTS), 4)
        if days
        else 0,
        "avg_hold_bars": round(float(np.mean(holds)), 1) if holds else 0,
        "orders_placed": placed,
        "fills": res.get("n_fills", 0),
        "maker_fill_rate": round(res.get("n_fills", 0) / placed, 4) if placed else 0,
        "touch_rejects": res["touch_rejects"],
        "capacity_events": res["capacity_events"],
        "exit_reasons": reasons,
    }


async def main(dry: bool, register: bool) -> None:
    n_trials = _load_trials()["total_trials"] + 1
    dsr_bar = _dsr_threshold(n_trials)
    print("=" * 78)
    print(
        f"TPB-001 trend_pullback_v1 — GLOBAL trial #{n_trials}  DSR门槛={dsr_bar:.4f}"
    )
    print("=" * 78)

    data = await build_dataset(GRID[0])
    days = 0.0
    for inst, d in data.items():
        span = (d.index[-1] - d.index[0]).total_seconds() / 86400
        days = max(days, span)
        print(
            f"  {inst}: {len(d)} 根 1h  {d.index[0]:%Y-%m-%d} → {d.index[-1]:%Y-%m-%d}"
        )
    print(f"  主窗口 {days:.0f} 天\n")

    # ── T1 中心配置 ──
    center = await run_config(GRID[0], data)
    g_pnl = _bar_pnl(center["gross_curve"])
    n_pnl = _bar_pnl(center["equity_curve"])
    gross_sr = _sharpe_ann(g_pnl)
    net_sr = _sharpe_ann(n_pnl)

    print("── T1 中心配置 ──")
    print(
        f"  交易数 {center['n_trades']}   gross Sharpe {gross_sr:+.3f}   net Sharpe {net_sr:+.3f}"
    )
    print("  成本分解(§5 C6):")
    print(f"    gross            {center['gross_pnl']:+10.2f}")
    print(f"    − maker          {center['maker_cost']:10.2f}")
    print(f"    − taker+滑点     {center['taker_cost']:10.2f}")
    print(f"    − funding        {center['funding_cost']:10.2f}")
    print(f"    = net            {center['net_pnl']:+10.2f}")

    # ── T2 KILL-1 ──
    if gross_sr <= 0:
        print(f"\n🔴 KILL-1 触发:组合 gross Sharpe {gross_sr:+.3f} ≤ 0 → 立即 REJECT")
        print("   按 §7 T2,不看任何后续指标。ATR追踪翻转族封棺。")
        verdict = "REJECT"
        metrics = {
            "overall": verdict,
            "kill_1": True,
            "gross_sharpe": round(gross_sr, 4),
            "net_sharpe": round(net_sr, 4),
            "n_trades": center["n_trades"],
            "cost_breakdown": {
                "gross": round(center["gross_pnl"], 2),
                "maker": round(center["maker_cost"], 2),
                "taker_slip": round(center["taker_cost"], 2),
                "funding": round(center["funding_cost"], 2),
                "net": round(center["net_pnl"], 2),
            },
            "freq": _freq_report(center, days),
            "crisis": _crisis_report(center),
            "note": (
                "KILL-1: gross Sharpe ≤ 0,按 §7 T2 立即 REJECT,未计算 "
                "DSR/PBO/扰动网格。窗口 2024-07-20→2026-07-10(1h数据所限,"
                "非 spec 期望的 ≥2021);funding veto OFF(F4)。"
            ),
        }
        _emit(verdict, metrics, center, None, n_trials, dsr_bar, register)
        return

    if dry:
        print("\n(--dry:仅 KILL-1 探针,不跑扰动网格)")
        return

    # ── T3 扰动网格 ──
    print("\n── T3 扰动网格(仅符号检验,不选优)──")
    results = [center]
    for cfg in GRID[1:]:
        r = await run_config(cfg, data)
        results.append(r)
    signs = []
    for cfg, r in zip(GRID, results):
        gs = _sharpe_ann(_bar_pnl(r["gross_curve"]))
        ns = _sharpe_ann(_bar_pnl(r["equity_curve"]))
        signs.append(gs > 0)
        print(f"  {cfg.label:<10} n={r['n_trades']:<5} gross {gs:+.3f}  net {ns:+.3f}")
    n_pos = sum(signs)
    print(f"  → gross>0: {n_pos}/7  (判据 ≥6/7)")

    # ── T4 验证层 ──
    print("\n── T4 验证层(CPCV / PBO / DSR)──")
    gate = _walk_forward_gate(
        n_pnl,
        N_SPLITS,
        EMBARGO_BARS,
        PERIODS_1H,
        pbo_threshold=PBO_THRESHOLD,
        purge_bars=PURGE_BARS,
    )
    dsr_val = gate["deflated_sharpe"]
    pbo_cscv = gate["pbo_cscv"]
    pbo_cross = _cross_config_pbo(results)
    per_trade = np.asarray(
        [
            t.net_pnl / (t.entry_px * t.qty)
            for t in center["trades"]
            if t.entry_px * t.qty > 0
        ]
    )
    dsr_real = _deflated_sharpe_real(n_pnl, gate["oos_sharpes"], n_trials, PERIODS_1H)
    print(
        f"  mean_oos {gate['mean_oos_sharpe']:+.3f}  DSR(mean−std) {dsr_val:+.3f}  "
        f"门槛 {dsr_bar:.3f}  adj {dsr_val - dsr_bar:+.3f}"
    )
    print(
        f"  PBO_cscv {pbo_cscv:.3f}   PBO_跨配置(F5) {pbo_cross:.3f}   "
        f"IS>OOS频率 {gate['pbo']:.3f}"
    )
    print(
        f"  Bailey-LdP 真DSR概率(non-gating) {dsr_real:.4f}   每笔收益 n={len(per_trade)}"
    )

    # 前后半段符号一致性
    half = len(n_pnl) // 2
    sr_h1, sr_h2 = _sharpe_ann(n_pnl[:half]), _sharpe_ann(n_pnl[half:])
    print(
        f"  前后半段 net Sharpe: {sr_h1:+.3f} / {sr_h2:+.3f}  "
        f"符号{'一致' if (sr_h1 > 0) == (sr_h2 > 0) else '不一致'}"
    )

    # 每资产 net
    per_asset = {}
    for inst in INSTRUMENTS:
        tr = [t for t in center["trades"] if t.instrument == inst]
        per_asset[inst] = round(sum(t.net_pnl for t in tr), 2)
    n_asset_pos = sum(1 for v in per_asset.values() if v > 0)
    print(f"  每资产 net: {per_asset}  → 为正 {n_asset_pos}/3 (判据 ≥2/3)")

    # ── T5/T6 ──
    crisis = _crisis_report(center)
    freq = _freq_report(center, days)
    print("\n── T5 危机窗 ──")
    for c in crisis:
        print(
            f"  {c['window']}: 入场 {c['n_entries']}  net {c['net_pnl']:+.2f}  {c['note']}"
        )
    print("\n── T6 频率 ──")
    print(
        f"  {freq['n_trades']} 笔 / {freq['trades_per_day_per_asset']} 笔·天⁻¹·资产⁻¹  "
        f"平均持仓 {freq['avg_hold_bars']} 根"
    )
    print(
        f"  挂单 {freq['orders_placed']} → 成交 {freq['fills']} "
        f"(maker成交率 {freq['maker_fill_rate']:.1%})  touch拒 {freq['touch_rejects']}"
    )
    print(f"  出场原因: {freq['exit_reasons']}")

    # ── §8 判决 ──
    checks = {
        "DSR > 门槛": dsr_val > dsr_bar,
        "PBO_cscv < 0.5": (not math.isnan(pbo_cscv)) and pbo_cscv < PBO_THRESHOLD,
        "net Sharpe > 0": net_sr > 0,
        "≥2/3 资产 net > 0": n_asset_pos >= 2,
        "扰动 ≥6/7 gross>0": n_pos >= 6,
    }
    verdict = "PASS" if all(checks.values()) else "REJECT"
    print("\n" + "=" * 78)
    for k, v in checks.items():
        print(f"  {'✓' if v else '✗'} {k}")
    print(f"  §8 判决(N={n_trials}): {verdict}")
    print("=" * 78)

    metrics = {
        "overall": verdict,
        "kill_1": False,
        "gross_sharpe": round(gross_sr, 4),
        "net_sharpe": round(net_sr, 4),
        "dsr": round(dsr_val, 4),
        "dsr_threshold": round(dsr_bar, 4),
        "adj_dsr": round(dsr_val - dsr_bar, 4),
        "pbo_cscv": round(pbo_cscv, 4) if not math.isnan(pbo_cscv) else None,
        "pbo_cross_config": round(pbo_cross, 4) if not math.isnan(pbo_cross) else None,
        "dsr_real_nongating": round(dsr_real, 4) if not math.isnan(dsr_real) else None,
        "n_trades": center["n_trades"],
        "per_asset_net": per_asset,
        "perturbation_gross_pos": f"{n_pos}/7",
        "halves_sharpe": [round(sr_h1, 3), round(sr_h2, 3)],
        "cost_breakdown": {
            "gross": round(center["gross_pnl"], 2),
            "maker": round(center["maker_cost"], 2),
            "taker_slip": round(center["taker_cost"], 2),
            "funding": round(center["funding_cost"], 2),
            "net": round(center["net_pnl"], 2),
        },
        "crisis": crisis,
        "freq": freq,
        "checks": {k: bool(v) for k, v in checks.items()},
        "note": (
            "窗口 2024-07-20→2026-07-10(719d,1h数据所限,非 spec 期望的 ≥2021);"
            "FTX-2022 1h 无覆盖故排除;funding veto OFF(F4,dydx 仅覆盖窗口34%);"
            "embargo=50/purge=48(F6/F6b,非 v51 字面默认0)。"
        ),
    }
    _emit(verdict, metrics, center, results, n_trials, dsr_bar, register)


def _emit(verdict, metrics, center, results, n_trials, dsr_bar, register) -> None:
    out = Path("ops/reports")
    out.mkdir(parents=True, exist_ok=True)
    p = out / "tpb_v1_result.json"
    p.write_text(json.dumps(metrics, ensure_ascii=False, indent=2, default=str))
    print(f"\n结果 JSON → {p}")
    if register:
        tn = _save_trial(
            "ops/scripts/tpb_v1_gate.py (TPB-001 trend_pullback_v1)", verdict, metrics
        )
        print(f".gate_trials.json 已登记 trial #{tn}  verdict={verdict}")
    else:
        print("(未登记 —— 加 --register 才写入 .gate_trials.json。登记不可逆。)")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true", help="仅跑中心配置做 KILL-1 探针")
    ap.add_argument(
        "--register", action="store_true", help="把判决写入 .gate_trials.json(不可逆)"
    )
    a = ap.parse_args()
    asyncio.run(main(a.dry, a.register))
