"""独立 P&L 核算 —— helixa freqtrade-validator(用独立执行引擎复核 Nautilus 纸面
成交)的能力等价物。不是照搬 Freqtrade,按"只复刻能力"原则改成:用一条完全独立、
从零手写的核算代码路径,从 paper.fills 原始成交重算已实现 P&L,和 paper/risk.py
的 realized_pnl() 对账——两条路径算出的数字不一致,说明其中一条有 bug。

背景:2026-07-11(commit 8987a32)paper/gateway 的读取侧曾经漏乘 ctVal,把 BTC 的
P&L/NAV 放大 100 倍、ETH 放大 10 倍,而且是悄悄发生的——两条路径当时都是同一处
bug 的下游,谁也没能验出问题。这个脚本的价值就在于"独立"两个字:
  - ctVal 常量在本文件本地硬编码,不 import paper.contracts.CT_VAL —— 那份文件
    本身出错时(哪怕只是抄错一个数字)这里也不会跟着错。
  - 均价法(avg-cost)配对逻辑重新手写,不复用 paper/risk.py 的 realized_pnl()/
    open_positions() 实现。

用法:
    python ops/scripts/pnl_reconcile.py [--once]
不一致(超过 $0.01 容差)时,写 paper.risk_events(kind=pnl_reconcile_mismatch,
severity=high)并以非零退出码结束,方便接 systemd OnFailure 告警。
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

import asyncpg

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

HV_DSN = os.environ.get(
    "DB_DSN", "postgresql://helios:helios_dev_pass@localhost:5434/helivex"
)

# 独立硬编码,故意不 import paper.contracts.CT_VAL —— 见模块docstring。
_CT_VAL: dict[str, float] = {
    "BTC-USDT-SWAP": 0.01,
    "ETH-USDT-SWAP": 0.1,
    "SOL-USDT-SWAP": 1.0,
}
TOLERANCE_USD = 0.01


def _ct_val(instrument: str) -> float:
    return _CT_VAL.get(instrument.split(".")[0], 1.0)


def independent_realized_pnl(
    fills: list[dict],
) -> tuple[float, dict[tuple[str, str], float]]:
    """从原始成交重算已实现 P&L(均价法,按 strategy_id+instrument 分组独立配对)。

    与 paper/risk.py 的 realized_pnl() 采用同一种配对方法(均价法,而非 FIFO)——
    这是刻意的:两条路径方法论不同的话,partial-close 场景下数字必然不一致,
    对账会一直"误报";只有同方法、独立实现,不一致才纯粹指向代码 bug。
    """
    per_key: dict[tuple[str, str], list[float]] = {}  # key -> [signed_qty, avg_cost]
    realized_by_key: dict[tuple[str, str], float] = {}
    total = 0.0

    for f in fills:
        key = (f["strategy_id"], f["instrument"])
        ctval = _ct_val(f["instrument"])
        q, cost = per_key.get(key, [0.0, 0.0])
        fill_q = float(f["quantity"]) * (1.0 if f["side"] == "BUY" else -1.0)
        px = float(f["actual_fill_price"])

        if q == 0 or (q > 0) == (fill_q > 0):
            # 加仓(或开新仓):滚动更新均价,不产生已实现盈亏
            new_q = q + fill_q
            cost = (
                (cost * abs(q) + px * abs(fill_q)) / abs(new_q) if new_q != 0 else 0.0
            )
            q = new_q
        else:
            # 减仓/反手:按均价对减仓部分结算已实现盈亏(乘 ctVal 转真实币量)
            closing_qty = min(abs(fill_q), abs(q))
            pnl = closing_qty * ctval * (px - cost) * (1.0 if q > 0 else -1.0)
            realized_by_key[key] = realized_by_key.get(key, 0.0) + pnl
            total += pnl
            q += fill_q
            if q == 0:
                cost = 0.0
            elif (q > 0) != (q - fill_q > 0):
                # 反手:剩余仓位以本次成交价重新开仓
                cost = px

        per_key[key] = [q, cost]

    return total, realized_by_key


async def run_once(hv: asyncpg.Pool) -> dict:
    from paper.risk import log_risk_event, realized_pnl

    async with hv.acquire() as conn:
        fills = await conn.fetch(
            """SELECT strategy_id, instrument, side, quantity, actual_fill_price
               FROM paper.fills ORDER BY ts ASC"""
        )
        official_total = await realized_pnl(conn)

    independent_total, by_key = independent_realized_pnl([dict(r) for r in fills])
    diff = independent_total - official_total
    mismatch = abs(diff) > TOLERANCE_USD

    result = {
        "official_total": official_total,
        "independent_total": independent_total,
        "diff": diff,
        "mismatch": mismatch,
        "n_fills": len(fills),
        "by_strategy_instrument": {
            f"{k[0]}/{k[1]}": round(v, 4) for k, v in by_key.items()
        },
    }

    if mismatch:
        async with hv.acquire() as conn:
            await log_risk_event(
                conn,
                kind="pnl_reconcile_mismatch",
                entity_id="pnl_reconcile",
                severity="high",
                message=(
                    f"独立重算 P&L 与 paper.risk.realized_pnl 不一致: "
                    f"official={official_total:.4f} independent={independent_total:.4f} "
                    f"diff={diff:.4f}(容差 ${TOLERANCE_USD})"
                ),
                metrics=result,
            )
    return result


async def main(once: bool) -> None:
    hv = await asyncpg.create_pool(HV_DSN, min_size=1, max_size=2)
    try:
        r = await run_once(hv)
        status = "MISMATCH" if r["mismatch"] else "OK"
        print(
            f"pnl_reconcile: {status}  official={r['official_total']:.4f} "
            f"independent={r['independent_total']:.4f} diff={r['diff']:.4f} "
            f"n_fills={r['n_fills']}"
        )
        if r["mismatch"]:
            for k, v in r["by_strategy_instrument"].items():
                print(f"  {k}: independent_realized={v}")
            sys.exit(1)
    finally:
        await hv.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--once", action="store_true", help="run a single reconciliation and exit"
    )
    args = parser.parse_args()
    asyncio.run(main(args.once))
