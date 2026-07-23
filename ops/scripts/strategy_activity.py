#!/usr/bin/env python3
"""策略活跃度快检 —— 一眼区分「没信号(正常观望)」和「真卡住」。

背景:趋势/突破策略(donchian 4H、trend_follower/spot_trend 1D、futures_signal)隔几天
不开单是常态(震荡市信号全 NEUTRAL,正确地待着);但重启清零 bar 聚合、NT 崩溃、熔断
误触发也会让策略静默。肉眼看「没开单」两者分不清 —— 本检自动分类:

  STUCK    超过 ~2.5× 正常信号间隔没发信号 → 真报警(引擎/bar 聚合/崩溃)
  WAITING  按节奏在评估但近 24h 全 NEUTRAL → 正常(市场没给信号)
  BLOCKED? 有 actionable 信号但 0 成交 → gate/risk/冷却吃了(或只是无仓可平的 exit),需查
  ACTIVE   近 24h 有成交,正常交易
  NEW      信号太少,节奏未知,不判 stuck

节奏(expected gap)从该策略自身信号历史的中位间隔算出,不依赖 config —— 连 id 里不带
周期的 *_port 策略也能判。全局熔断态单列(熔断=所有入场被挡,会让全体 0 成交)。

用法:  ./venv/bin/python ops/scripts/strategy_activity.py [--json]
退出码:有任何 STUCK → 2;有 BLOCKED? → 1;全正常 → 0(可接告警/timer)。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import urllib.request
from datetime import datetime, timezone

import asyncpg

DSN = "postgresql://helios:helios_dev_pass@localhost:5434/helivex"
GATEWAY = "http://127.0.0.1:8765"
STUCK_MULT = 2.5  # staleness > STUCK_MULT × 正常间隔 → STUCK
STUCK_FLOOR_S = 1800  # 绝对下限 30min:防高频策略(5m)偶尔一根 bar 延迟就假 STUCK
MIN_SIGNALS = 6  # 少于这么多信号 → 节奏未知,不判 stuck

# 权威 bar 间隔(秒),按 signal-id 前缀匹配。用 config 口径而非从信号历史推断 —— 后者会被
# 重启/部署时的信号爆发(几十条挤在几分钟)污染中位数,把日线策略误判成分钟级 → 假 STUCK。
# (donchian=4H / vwap_mr=1h / futures_signal_port≈1h / scalp_5m=5m / scalper_v2_port≈5m /
#  spot_trend_1d=1D / trend_follower_port=1D / ler_okx=1m,与实测 cadence 一致。)
TF_SECONDS = {
    "ler_okx": 60,
    "scalp_5m": 300,
    "scalper_v2_port": 300,
    "vwap_mr_1h": 3600,
    "futures_signal_port": 3600,
    "donchian_4h": 14400,
    "spot_trend_1d": 86400,
    "trend_follower_port": 86400,
}


def _expected_gap(strategy_id: str) -> int | None:
    """按前缀取权威 bar 间隔;无匹配 → None(交给历史推断兜底)。"""
    for prefix, secs in TF_SECONDS.items():
        if strategy_id.startswith(prefix):
            return secs
    return None


def _fmt_age(secs: float) -> str:
    if secs < 90:
        return f"{secs:.0f}s"
    if secs < 5400:
        return f"{secs / 60:.0f}m"
    if secs < 172800:
        return f"{secs / 3600:.1f}h"
    return f"{secs / 86400:.1f}d"


def _kill_switch() -> dict | None:
    try:
        with urllib.request.urlopen(f"{GATEWAY}/risk/status", timeout=8) as r:
            d = json.load(r)
        return {
            "tripped": d["kill_switch"]["tripped"],
            "dd": d.get("drawdown_pct"),
            "reason": d["kill_switch"].get("reason", ""),
        }
    except Exception as e:  # noqa: BLE001
        return {"error": f"{type(e).__name__}: {e}"}


async def analyze() -> list[dict]:
    conn = await asyncpg.connect(DSN)
    now = datetime.now(timezone.utc)
    # 近 30 天有信号的策略;取每策略近 40 条信号时间(算节奏)+ 近 24h 统计
    strat_ids = [
        r["strategy_id"]
        for r in await conn.fetch(
            "SELECT DISTINCT strategy_id FROM paper.signals WHERE ts > now()-interval '7 days'"
        )
    ]
    out = []
    for sid in strat_ids:
        sig = await conn.fetch(
            "SELECT ts, action FROM paper.signals WHERE strategy_id=$1 ORDER BY ts DESC LIMIT 40",
            sid,
        )
        if not sig:
            continue
        last_ts = sig[0]["ts"]
        staleness = (now - last_ts).total_seconds()
        # 正常信号间隔:权威 bar 间隔优先(免疫重启爆发);无匹配才用历史中位数兜底
        ts_list = [r["ts"] for r in sig]
        gaps = [
            (ts_list[i] - ts_list[i + 1]).total_seconds()
            for i in range(len(ts_list) - 1)
        ]
        exp_gap = _expected_gap(sid) or (statistics.median(gaps) if gaps else None)
        # 近 24h actionable + fills
        act_24h = await conn.fetchval(
            "SELECT count(*) FROM paper.signals WHERE strategy_id=$1 AND ts > now()-interval '24 hours' AND action<>'NEUTRAL'",
            sid,
        )
        sig_24h = await conn.fetchval(
            "SELECT count(*) FROM paper.signals WHERE strategy_id=$1 AND ts > now()-interval '24 hours'",
            sid,
        )
        fill_24h = await conn.fetchval(
            "SELECT count(*) FROM paper.fills WHERE strategy_id=$1 AND ts > now()-interval '24 hours'",
            sid,
        )
        # 分类
        if len(sig) < MIN_SIGNALS or exp_gap is None or exp_gap <= 0:
            status = "NEW"
        elif exp_gap and staleness > max(STUCK_MULT * exp_gap, STUCK_FLOOR_S):
            status = "STUCK"
        elif fill_24h > 0:
            status = "ACTIVE"
        elif act_24h > 0:
            status = "BLOCKED?"
        else:
            status = "WAITING"
        out.append(
            {
                "strategy_id": sid,
                "status": status,
                "last_signal": last_ts.isoformat(),
                "staleness_s": round(staleness),
                "expected_gap_s": round(exp_gap) if exp_gap else None,
                "sig_24h": sig_24h,
                "act_24h": act_24h,
                "fill_24h": fill_24h,
            }
        )
    await conn.close()
    order = {"STUCK": 0, "BLOCKED?": 1, "WAITING": 2, "ACTIVE": 3, "NEW": 4}
    out.sort(key=lambda r: (order.get(r["status"], 9), r["strategy_id"]))
    return out


async def main(as_json: bool) -> int:
    rows = await analyze()
    ks = _kill_switch()

    if as_json:
        print(
            json.dumps(
                {"kill_switch": ks, "strategies": rows}, ensure_ascii=False, indent=2
            )
        )
    else:
        print("=" * 92)
        print(
            f"策略活跃度快检  {datetime.now(timezone.utc).isoformat(timespec='seconds')}"
        )
        if ks and ks.get("tripped"):
            print(
                f"  🛑 熔断 TRIPPED (dd {ks.get('dd')}%) —— 所有入场被挡!reason: {str(ks.get('reason'))[:80]}"
            )
        elif ks and "error" in ks:
            print(f"  ⚠ 熔断态取不到:{ks['error']}")
        else:
            print(f"  ✅ 风控正常 (dd {ks.get('dd') if ks else '?'}%)")
        print("=" * 92)
        icon = {
            "STUCK": "🔴",
            "BLOCKED?": "🟠",
            "WAITING": "⚪",
            "ACTIVE": "🟢",
            "NEW": "🔵",
        }
        print(
            f"  {'':2} {'strategy':40s} {'last_sig':>9} {'~gap':>7} {'24h: sig/act/fill':>18}"
        )
        for r in rows:
            print(
                f"  {icon.get(r['status'], '?')} {r['status']:8s} {r['strategy_id']:31s} "
                f"{_fmt_age(r['staleness_s']):>9} {_fmt_age(r['expected_gap_s']) if r['expected_gap_s'] else '—':>7} "
                f"{r['sig_24h']:>5}/{r['act_24h']:>3}/{r['fill_24h']:<3}"
            )
        n_stuck = sum(1 for r in rows if r["status"] == "STUCK")
        n_block = sum(1 for r in rows if r["status"] == "BLOCKED?")
        print("=" * 92)
        print(
            f"  🔴 STUCK={n_stuck}  🟠 BLOCKED?={n_block}  "
            f"🟢 ACTIVE={sum(1 for r in rows if r['status'] == 'ACTIVE')}  "
            f"⚪ WAITING={sum(1 for r in rows if r['status'] == 'WAITING')}"
        )
        if n_stuck:
            print(
                "  🔴 STUCK = 真报警:该策略超过 2.5× 正常间隔没发信号(查重启/bar聚合/NT崩溃)"
            )
        if n_block:
            print(
                "  🟠 BLOCKED? = 有 actionable 信号但 0 成交:查 gate/risk/止损冷却(或只是无仓可平)"
            )

    n_stuck = sum(1 for r in rows if r["status"] == "STUCK")
    n_block = sum(1 for r in rows if r["status"] == "BLOCKED?")
    return 2 if n_stuck else (1 if n_block else 0)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    sys.exit(asyncio.run(main(ap.parse_args().json)))
