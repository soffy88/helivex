#!/usr/bin/env python3
"""
R5.4: SOL 1H VWAP-MR — post-only maker gate. (R5.3 §7 预注册的下一步,trial #30)

R5.3 结论:SOL 1H 是 29 次试验里唯一费后翻正过的组合(gross +1.155,@5bps RT +0.615,
@10bps taker +0.074 但 gate FAIL on variance)。当时 5bps RT 是"假设 maker 成交"的
算术,成交率与逆向选择都没建模 —— scalp_5m maker 实验(07-21 起)已实测证明 R5 的
97% maker 假设是错的(5m 贴盘口:BTC 90% / ETH 71% / SOL 50%,SOL 报价陈旧率还把
一半入场踢回了 taker)。

本试验把 maker 执行诚实地内生化,不再套外生成交率:

  信号 bar i 收盘发信号 → 限价单挂 close[i](≈贴同侧最优价,镜像 scalp_5m._passive_px)
  → 挂 timeout_bars 根 bar:
      BUY  在 bar j 成交 ⟺ low[j]  < close[i](严格穿过 —— 只触及不保证队列成交)
      SELL 在 bar j 成交 ⟺ high[j] > close[i]
  → 超时不成交:入场作废(复刻线上 scalp maker 实验;入场无 taker 兜底)
  → 出场:hold 根 bar 后时间平仓,按 taker 5bps(TPB-001:出场 88% 走 taker;保守)
  → 入场 maker 2bps ⇒ 基准 RT = 7bps;敏感性 {4bps 双腿maker, 7bps, 10bps 全taker}

逆向选择因此是模型自带的:限价单恰好在价格继续逆行时成交,立即反弹的"好信号"
反而不成交 —— 这正是 maker 假设历来高估收益的机制,R5.4 直接测它值多少 bps。

信号双变体(都跑 gate,预注册主判定 = A):
  A  R5.3 原版:vwap_n=4,σ 同窗(4 样本)—— 与 +1.155 gross 可比,是 R5.4 预注册假设
  B  线上部署版:vwap_n=4,std_n=24(σ 解耦,2026-07-22 修复)—— 部署相关性

数据:okx_swap_1h 真实 1h(719 天,2024-07-20 → 2026-07-10)。R5.3 当时用的 5.4 年
5m 历史已不在库中(ohlcv_5m 只保留 2026-04 起的滚动窗口),因此 R5.3 的 +1.155 gross
不可复刻;本试验窗口与 2026-07-22 诊断 §5.1 的出场重设计回放完全同源,可交叉校验
(该回放:σ4@10bps taker ≈ -2.9,σ24 ≈ -1.8,入场口径=信号 bar 收盘,略异于本模型的
次 bar 限价)。窗口覆盖 Bull2024b / Chop2025 / Bear2025 / 2026H1 四个 regime。
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

import asyncpg
import numpy as np
import pandas as pd

PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
_3O = PROJECT_ROOT.parent / "platform" / "3O"
for _pkg in ("oprim", "oskill", "omodul"):
    sys.path.insert(0, str(_3O / _pkg))

from omodul.backtest_gate import BacktestGateConfig, backtest_gate  # noqa: E402

sys.path.insert(0, str(PROJECT_ROOT / "tools"))
from strategy_gate import _dsr_threshold, _load_trials, _save_trial  # noqa: E402

DB_DSN = "postgresql://helios:helios_dev_pass@localhost:5434/helivex"
INSTRUMENT = "SOL-USDT-SWAP"
SOURCE_1H = "okx_swap_1h"  # 真实 1h,719 天(唯一现存的 1H 级历史)

PERIODS_1H = 8_760
FOLD_MIN_1H = 2_880  # ~4 个月/折 → 719 天出 6 折(项目 gate 惯例 n_splits=6)
CONTEXT_BARS = 72
EMBARGO_BARS = 48

HOLD = 6
Z_THR = 2.0
VWAP_N = 4
TIMEOUT_BARS = 1  # 挂单等 1 根 1H bar,不成交作废(镜像 scalp maker_timeout_bars=1)

MAKER_BPS = 2.0  # OKX 标准档
TAKER_BPS = 5.0
# (入场bps, 出场bps, 标签)。基准 = maker 入场 + taker 出场。
COST_SCENARIOS = [
    (MAKER_BPS, MAKER_BPS, "4bps 双腿maker(乐观界)"),
    (MAKER_BPS, TAKER_BPS, "7bps maker入+taker出(基准)"),
    (TAKER_BPS, TAKER_BPS, "10bps 全taker(参照)"),
]
GATE_ENTRY_BPS, GATE_EXIT_BPS = MAKER_BPS, TAKER_BPS

VARIANTS = {
    "A_r53_sigma4": {"vwap_n": VWAP_N, "std_n": VWAP_N},  # R5.3 原版(σ 同窗)
    "B_live_sigma24": {"vwap_n": VWAP_N, "std_n": 24},  # 线上部署版(σ 解耦)
}
PRIMARY = "A_r53_sigma4"  # 预注册主判定(R5.3 §7 写下的 R5.4 就是这个信号)

CYCLES = [  # 数据窗内的 regime 分段(R5.3 同名分段 + 2026H1)
    ("Bull2024b", "2024-10-01", "2025-01-31"),
    ("Chop2025", "2025-02-01", "2025-07-31"),
    ("Bear2025", "2025-08-01", "2026-01-31"),
    ("2026H1", "2026-02-01", "2026-07-10"),
]


# ── data ──────────────────────────────────────────────────────────────────────


async def load_1h() -> pd.DataFrame:
    conn = await asyncpg.connect(DB_DSN)
    rows = await conn.fetch(
        """SELECT bar_close_ts, open::float, high::float, low::float,
                  close::float, volume::float
           FROM market_data.ohlcv_1h
           WHERE instrument=$1 AND source=$2 ORDER BY bar_close_ts""",
        INSTRUMENT,
        SOURCE_1H,
    )
    await conn.close()
    df = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "volume"])
    df["ts"] = pd.to_datetime(df["ts"], utc=True)
    return df.set_index("ts").sort_index()


# ── signal(R5.3 同式,σ 窗口可解耦)─────────────────────────────────────────


def signals_vwap_mr(
    closes: np.ndarray, volumes: np.ndarray, vwap_n: int, std_n: int, z_thr: float
) -> np.ndarray:
    s_c = pd.Series(closes, dtype=float)
    s_v = pd.Series(volumes, dtype=float)
    minp = max(2, vwap_n // 4)
    roll_vc = (s_c * s_v).rolling(vwap_n, min_periods=minp).sum().shift(1)
    roll_v = s_v.rolling(vwap_n, min_periods=minp).sum().shift(1)
    vwap = roll_vc / (roll_v + 1e-10)
    minp_s = max(2, std_n // 4) if std_n != vwap_n else minp
    std = s_c.rolling(std_n, min_periods=minp_s).std().shift(1)
    z = (s_c - vwap) / (std + 1e-10)
    z_vals = z.values
    sigs = np.zeros(len(closes), dtype=int)
    sigs[z_vals > z_thr] = -1
    sigs[z_vals < -z_thr] = +1
    sigs[np.isnan(z_vals)] = 0
    return sigs


def annualized_sharpe(rets, periods: int) -> float:
    arr = np.asarray(rets, dtype=float)
    if len(arr) < 5 or arr.std() < 1e-10:
        return 0.0
    return float(arr.mean() / arr.std() * np.sqrt(periods))


# ── maker execution sim ──────────────────────────────────────────────────────


def sim_maker(
    closes: np.ndarray,
    highs: np.ndarray,
    lows: np.ndarray,
    signals: np.ndarray,
    hold: int,
    entry_bps: float,
    exit_bps: float,
    timeout_bars: int = TIMEOUT_BARS,
) -> dict:
    """Post-only 限价入场:挂 close[i],严格穿过才成交,超时作废;taker 时间平仓。

    返回逐 bar 收益序列 + 成交/作废统计 + 逆向选择诊断(成交 vs 作废信号的
    后续 hold 根 bar 毛收益差)。
    """
    n = len(closes)
    entry_cost = entry_bps / 10_000
    exit_cost = exit_bps / 10_000
    rets = np.zeros(n)

    pos = 0  # 当前持仓方向
    entry_px = 0.0
    bars_left = 0
    resting_dir = 0  # 挂单方向(0=无挂单)
    resting_px = 0.0
    resting_age = 0

    n_signals = 0
    n_filled = 0
    n_expired = 0
    filled_fwd: list[float] = []  # 成交信号的 hold 根 bar 毛收益(方向化)
    missed_fwd: list[float] = []  # 作废信号的同口径反事实收益

    def _fwd_gross(i: int, d: int, px: float) -> float | None:
        j = min(i + hold, n - 1)
        if j <= i or px <= 0:
            return None
        return d * (closes[j] - px) / px

    for i in range(1, n):
        # ① 已有持仓:先记本 bar 持仓收益,到期 taker 平仓
        if pos != 0:
            prev = entry_px if bars_left == hold else closes[i - 1]
            if prev > 0:
                rets[i] += pos * (closes[i] - prev) / prev
            bars_left -= 1
            if bars_left == 0:
                rets[i] -= exit_cost
                pos = 0
            continue  # 持仓期间不处理挂单/新信号(线上同:_position!=0 直接 return)

        # ② 有挂单:本 bar 检查是否穿价成交
        if resting_dir != 0:
            crossed = lows[i] < resting_px if resting_dir > 0 else highs[i] > resting_px
            if crossed:
                pos = resting_dir
                entry_px = resting_px
                bars_left = hold
                n_filled += 1
                rets[i] -= entry_cost
                # 成交 bar 本身的持仓收益:fill_px → close[i]
                rets[i] += pos * (closes[i] - entry_px) / entry_px
                fg = _fwd_gross(i, pos, entry_px)
                if fg is not None:
                    filled_fwd.append(fg)
                bars_left -= 1  # 成交 bar 计为持有第 1 根
                if bars_left == 0:
                    rets[i] -= exit_cost
                    pos = 0
                resting_dir = 0
                continue
            resting_age += 1
            if resting_age >= timeout_bars:
                n_expired += 1
                mg = _fwd_gross(i, resting_dir, closes[i])  # 反事实:此刻市价追入
                if mg is not None:
                    missed_fwd.append(mg)
                resting_dir = 0
            continue  # 挂单期间不叠新信号

        # ③ flat 且无挂单:上一根 bar 的信号 → 挂限价 @ close[i-1]
        if signals[i - 1] != 0:
            n_signals += 1
            resting_dir = int(signals[i - 1])
            resting_px = closes[i - 1]
            resting_age = 0
            # 立刻检查本 bar(挂单后的第一根)是否成交
            crossed = lows[i] < resting_px if resting_dir > 0 else highs[i] > resting_px
            if crossed:
                pos = resting_dir
                entry_px = resting_px
                bars_left = hold
                n_filled += 1
                rets[i] -= entry_cost
                rets[i] += pos * (closes[i] - entry_px) / entry_px
                fg = _fwd_gross(i, pos, entry_px)
                if fg is not None:
                    filled_fwd.append(fg)
                bars_left -= 1
                if bars_left == 0:
                    rets[i] -= exit_cost
                    pos = 0
                resting_dir = 0
            elif timeout_bars <= 1:
                n_expired += 1
                mg = _fwd_gross(i, resting_dir, closes[i])
                if mg is not None:
                    missed_fwd.append(mg)
                resting_dir = 0
            else:
                resting_age = 1

    return {
        "returns": rets[1:].tolist(),
        "n_signals": n_signals,
        "n_filled": n_filled,
        "n_expired": n_expired,
        "fill_rate": n_filled / n_signals if n_signals else 0.0,
        "filled_fwd_bps": float(np.mean(filled_fwd)) * 10_000 if filled_fwd else None,
        "missed_fwd_bps": float(np.mean(missed_fwd)) * 10_000 if missed_fwd else None,
    }


def sim_taker_r53(closes, signals, hold, cost_oneway):
    """R5.3 原版 taker sim(复刻用,校验数据/信号没漂)。"""
    n = len(closes)
    pos = 0
    bars_left = 0
    n_trades = 0
    rets = []
    for i in range(1, n):
        if closes[i - 1] <= 0:
            rets.append(0.0)
            continue
        bar_ret = (closes[i] - closes[i - 1]) / closes[i - 1]
        cost = 0.0
        if pos == 0 and signals[i - 1] != 0:
            pos = int(signals[i - 1])
            bars_left = hold
            cost += cost_oneway
            n_trades += 1
        active = pos
        if pos != 0:
            bars_left -= 1
            if bars_left == 0:
                cost += cost_oneway
                pos = 0
        rets.append(active * bar_ret - cost)
    return rets, n_trades


# ── gate ─────────────────────────────────────────────────────────────────────


def make_strategy_fn(variant: dict):
    vn, sn = variant["vwap_n"], variant["std_n"]

    def strategy_fn(train_data: list, test_data: list) -> dict:
        def get(data, key):
            return np.array([d[key] for d in data], dtype=float)

        tr_c, tr_v = get(train_data, "close"), get(train_data, "volume")
        tr_h, tr_l = get(train_data, "high"), get(train_data, "low")
        te_c, te_v = get(test_data, "close"), get(test_data, "volume")
        te_h, te_l = get(test_data, "high"), get(test_data, "low")

        if len(tr_c) < CONTEXT_BARS or len(te_c) < HOLD:
            return {"sharpe": 0.0, "returns": [], "is_sharpe": 0.0}

        is_sigs = signals_vwap_mr(tr_c, tr_v, vn, sn, Z_THR)
        is_r = sim_maker(tr_c, tr_h, tr_l, is_sigs, HOLD, GATE_ENTRY_BPS, GATE_EXIT_BPS)

        full_c = np.concatenate([tr_c[-CONTEXT_BARS:], te_c])
        full_v = np.concatenate([tr_v[-CONTEXT_BARS:], te_v])
        full_h = np.concatenate([tr_h[-CONTEXT_BARS:], te_h])
        full_l = np.concatenate([tr_l[-CONTEXT_BARS:], te_l])
        full_sigs = signals_vwap_mr(full_c, full_v, vn, sn, Z_THR)
        oos_sigs = full_sigs[CONTEXT_BARS:]
        oos_r = sim_maker(
            te_c, te_h, te_l, oos_sigs, HOLD, GATE_ENTRY_BPS, GATE_EXIT_BPS
        )

        return {
            "sharpe": annualized_sharpe(oos_r["returns"], PERIODS_1H),
            "returns": oos_r["returns"],
            "is_sharpe": annualized_sharpe(is_r["returns"], PERIODS_1H),
        }

    return strategy_fn


def run_gate(df: pd.DataFrame, name: str, variant: dict) -> dict:
    n_splits = max(4, min(8, len(df) // FOLD_MIN_1H))
    records = [
        {
            "close": float(r.close),
            "volume": float(r.volume),
            "high": float(r.high),
            "low": float(r.low),
        }
        for r in df.itertuples()
    ]
    config = BacktestGateConfig(
        strategy_name=f"vwap_mr_maker_1h_{name}_{INSTRUMENT}",
        n_splits=n_splits,
        embargo=EMBARGO_BARS,
        pbo_threshold=0.5,
        periods=PERIODS_1H,
    )
    gate = backtest_gate(make_strategy_fn(variant), records, config=config)
    frs = gate["walk_forward_result"]["fold_results"]
    return {
        "gate_status": gate["gate_status"],
        "oos_sharpe": round(float(gate["mean_oos_sharpe"]), 3),
        "dsr": round(float(gate["deflated_sharpe"]), 3),
        "dsr_real": round(float(gate.get("deflated_sharpe_real", float("nan"))), 4)
        if gate.get("deflated_sharpe_real") is not None
        else None,
        "pbo": round(float(gate["pbo"]), 3),
        "pbo_cscv": round(float(gate["pbo_cscv"]), 3)
        if gate.get("pbo_cscv") is not None
        else None,
        "n_splits": n_splits,
        "fold_oos": [round(float(fr.get("sharpe", 0)), 3) for fr in frs],
        "fold_is": [round(float(fr.get("is_sharpe", 0)), 3) for fr in frs],
    }


# ── main ─────────────────────────────────────────────────────────────────────


def main() -> None:
    print("=== R5.4 SOL 1H VWAP-MR post-only maker gate ===\n")
    t0 = time.monotonic()
    df = asyncio.run(load_1h())
    c = df["close"].values.astype(float)
    h = df["high"].values.astype(float)
    lo = df["low"].values.astype(float)
    v = df["volume"].values.astype(float)
    n_years = len(df) / PERIODS_1H
    print(
        f"data: {len(df):,} 1H bars  {df.index[0].date()} → {df.index[-1].date()}"
        f"  ({n_years:.1f}y)  [{time.monotonic() - t0:.1f}s]\n"
    )

    # ── ① taker 参照(校验 vs 07-22 诊断 §5.1 回放:σ4@10bps≈-2.9 / σ24≈-1.8)──
    sigs_a = signals_vwap_mr(c, v, VWAP_N, VWAP_N, Z_THR)
    sigs_b = signals_vwap_mr(c, v, VWAP_N, 24, Z_THR)
    for name, sigs, ref in [("σ4", sigs_a, "-2.9"), ("σ24", sigs_b, "-1.8")]:
        rets, nt = sim_taker_r53(c, sigs, HOLD, 10 / 20_000)
        print(
            f"taker@10bps 参照 {name}: Sharpe {annualized_sharpe(rets, PERIODS_1H):+.3f}"
            f"  (§5.1 回放 ref {ref},口径略异)  trades={nt}"
        )
    print()

    # ── ② maker 执行:双变体 × 成本场景 ──
    results: dict[str, dict] = {}
    for name, var in VARIANTS.items():
        sigs = signals_vwap_mr(c, v, var["vwap_n"], var["std_n"], Z_THR)
        results[name] = {"scenarios": {}}
        print(f"── variant {name} (vwap_n={var['vwap_n']}, std_n={var['std_n']}) ──")
        for entry_bps, exit_bps, label in COST_SCENARIOS:
            r = sim_maker(c, h, lo, sigs, HOLD, entry_bps, exit_bps)
            sh = annualized_sharpe(r["returns"], PERIODS_1H)
            results[name]["scenarios"][label] = {
                "sharpe": round(sh, 3),
                "fill_rate": round(r["fill_rate"], 3),
                "n_signals": r["n_signals"],
                "n_filled": r["n_filled"],
            }
            print(
                f"  {label:32s} Sharpe {sh:+.3f}  fill {r['fill_rate'] * 100:.1f}%"
                f"  ({r['n_filled']}/{r['n_signals']},"
                f" {r['n_filled'] / (n_years * 365):.1f} 回合/天)"
            )
        # 逆向选择诊断(与成本无关,取基准场景的那次 sim)
        r = sim_maker(c, h, lo, sigs, HOLD, GATE_ENTRY_BPS, GATE_EXIT_BPS)
        results[name]["fill_rate"] = round(r["fill_rate"], 3)
        results[name]["adverse"] = {
            "filled_fwd_bps": round(r["filled_fwd_bps"], 2)
            if r["filled_fwd_bps"] is not None
            else None,
            "missed_fwd_bps": round(r["missed_fwd_bps"], 2)
            if r["missed_fwd_bps"] is not None
            else None,
        }
        fb, mb = r["filled_fwd_bps"], r["missed_fwd_bps"]
        if fb is not None and mb is not None:
            print(
                f"  逆向选择:成交信号 {HOLD}bar 毛收益 {fb:+.1f}bps"
                f" vs 作废信号(反事实市价追入){mb:+.1f}bps"
                f" → 选择成本 {mb - fb:.1f}bps/信号\n"
            )
        else:
            print(
                f"  逆向选择:作废样本不足(filled={fb} missed={mb},"
                f"作废 {r['n_expired']} 单)\n"
            )

    # ── ③ walk-forward gate(基准成本)──
    n_trial = _load_trials()["total_trials"] + 1
    print(
        f"── gate(maker入{GATE_ENTRY_BPS:.0f}+taker出{GATE_EXIT_BPS:.0f}bps,"
        f"trial #{n_trial},DSR bar={_dsr_threshold(n_trial):.3f})──"
    )
    for name, var in VARIANTS.items():
        t1 = time.monotonic()
        g = run_gate(df, name, var)
        results[name]["gate"] = g
        tag = "PASS" if g["gate_status"] == "passed" else "FAIL"
        print(
            f"  {name}: {tag}  OOS={g['oos_sharpe']:+.3f}  DSR={g['dsr']:+.3f}"
            f"  DSR_real={g['dsr_real']}  PBO={g['pbo']:.2f}"
            f"  CSCV={g['pbo_cscv']}  n={g['n_splits']}  [{time.monotonic() - t1:.0f}s]"
        )
        print(f"    IS:  {g['fold_is']}")
        print(f"    OOS: {g['fold_oos']}")
    print()

    # ── ④ per-cycle(基准成本,主变体)──
    print("── per-cycle(基准 7bps,variant A)──")
    cyc_rows = []
    for cname, start, end in CYCLES:
        mask = (df.index >= pd.Timestamp(start, tz="UTC")) & (
            df.index < pd.Timestamp(end, tz="UTC") + pd.Timedelta(days=1)
        )
        idx = np.where(mask)[0]
        if len(idx) < HOLD * 3:
            cyc_rows.append((cname, float("nan")))
            continue
        i0, i1 = idx[0], idx[-1] + 1
        r = sim_maker(
            c[i0:i1],
            h[i0:i1],
            lo[i0:i1],
            sigs_a[i0:i1],
            HOLD,
            GATE_ENTRY_BPS,
            GATE_EXIT_BPS,
        )
        sh = annualized_sharpe(r["returns"], PERIODS_1H)
        cyc_rows.append((cname, round(sh, 3)))
        print(f"  {cname:<12} {sh:+.3f}  {'✓' if sh > 0 else '✗'}")
    print()

    # ── ⑤ 落账 ──
    ga = results[PRIMARY]["gate"]
    verdict = "PASS" if ga["gate_status"] == "passed" else "FAIL"
    metrics = {
        "instruments": {
            INSTRUMENT: {
                "status": verdict,
                "dsr": ga["dsr"],
                "pbo": ga["pbo"],
                "pbo_cscv": ga["pbo_cscv"],
                "mean_oos": ga["oos_sharpe"],
                "fill_rate": results[PRIMARY]["fill_rate"],
                "adverse_selection": results[PRIMARY]["adverse"],
                "cost_model": f"maker入{GATE_ENTRY_BPS:.0f}bps+taker出{GATE_EXIT_BPS:.0f}bps,"
                f"限价穿越成交模型 timeout={TIMEOUT_BARS}bar",
            }
        },
        "overall": verdict,
        "secondary_variant_B_live_sigma24": {
            "status": results["B_live_sigma24"]["gate"]["gate_status"],
            "mean_oos": results["B_live_sigma24"]["gate"]["oos_sharpe"],
            "pbo_cscv": results["B_live_sigma24"]["gate"]["pbo_cscv"],
        },
        "cycles": dict(cyc_rows),
        "scenarios": {k: v["scenarios"] for k, v in results.items()},
    }
    tn = _save_trial("ops/scripts/vwap_mr_maker_gate_r54.py (R5.4)", verdict, metrics)
    print(f"R5.4 VERDICT (trial #{tn}): {verdict}")
    print("(verdict = 预注册主变体 A;B 仅报告)")


if __name__ == "__main__":
    main()
