#!/usr/bin/env python3
"""逐策略含费 FIFO 归因 —— 07-22 诊断 B1 承诺的常驻工具。

镜像 gateway/main.py:_round_trips 口径(FIFO、ctVal 张→币),在其上补两件事:
1. 归因按 (strategy_id, instrument) 分腿 —— 账户级 FIFO 会把跨策略对冲轧平成
   假利润(07-22 诊断 §1.2,差 $62.83),策略判断只能看这里的数。
2. 费用按 fill_type 估算(taker 5bps / maker 2bps 每边)—— 账本层 fees=0,
   费率恰恰是全组合最稳定的负收益流,不补上全部结论失真。

用法:
    venv/bin/python ops/scripts/paper_perf.py                  # 全历史
    venv/bin/python ops/scripts/paper_perf.py --since 2026-07-21
    venv/bin/python ops/scripts/paper_perf.py --open           # 附当前持仓归属
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import UTC, datetime
from collections import defaultdict, deque
from pathlib import Path

import asyncpg

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from paper.contracts import ct_val  # noqa: E402

DSN = "postgresql://helios:helios_dev_pass@localhost:5434/helivex"

FEE_BPS = {"maker": 2.0, "taker": 5.0}  # 每边;fill_type 缺失按 taker 保守估


def fee_usd(px: float, qty: float, ctv: float, fill_type: str | None) -> float:
    bps = FEE_BPS.get((fill_type or "taker").lower(), FEE_BPS["taker"])
    return px * qty * ctv * bps / 1e4


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default=None, help="起始日期(含),如 2026-07-21")
    ap.add_argument("--open", action="store_true", help="附当前未平持仓归属")
    args = ap.parse_args()

    conn = await asyncpg.connect(DSN)
    where = "WHERE ts >= $1" if args.since else ""
    params = (
        [datetime.fromisoformat(args.since).replace(tzinfo=UTC)] if args.since else []
    )
    rows = await conn.fetch(
        f"""SELECT ts, strategy_id, instrument, side, quantity,
                   actual_fill_price, fill_type
            FROM paper.fills {where} ORDER BY ts, id""",
        *params,
    )
    await conn.close()

    # 腿 = (strategy_id, instrument);FIFO 与 gateway/main.py:_round_trips 同构
    lots: dict[tuple, deque] = defaultdict(deque)  # leg -> deque[[qty_signed, px]]
    stat: dict[tuple, dict] = defaultdict(
        lambda: {
            "rounds": 0,
            "gross": 0.0,
            "fees": 0.0,
            "wins": 0,
            "worst": 0.0,
            "fills": 0,
            "maker": 0,
            "taker": 0,
        }
    )

    for r in rows:
        leg = (r["strategy_id"], r["instrument"])
        ctv = ct_val(r["instrument"])
        px = float(r["actual_fill_price"])
        q = float(r["quantity"]) * (1.0 if r["side"] == "BUY" else -1.0)
        s = stat[leg]
        s["fills"] += 1
        ft = (r["fill_type"] or "taker").lower()
        s["maker" if ft == "maker" else "taker"] += 1
        s["fees"] += fee_usd(px, abs(q), ctv, r["fill_type"])

        dq = lots[leg]
        round_gross = 0.0
        closed_any = False
        while abs(q) > 1e-12 and dq and (dq[0][0] > 0) != (q > 0):
            lot = dq[0]
            lot_sign = 1.0 if lot[0] > 0 else -1.0
            closed = min(abs(lot[0]), abs(q))
            round_gross += lot_sign * (px - lot[1]) * closed * ctv
            closed_any = True
            lot[0] -= lot_sign * closed
            q += lot_sign * closed
            if abs(lot[0]) < 1e-12:
                dq.popleft()
        if closed_any:
            s["rounds"] += 1
            s["gross"] += round_gross
            if round_gross > 0:
                s["wins"] += 1
            s["worst"] = min(s["worst"], round_gross)
        if abs(q) > 1e-12:
            dq.append([q, px])

    span = f"since {args.since}" if args.since else "全历史"
    print(f"逐策略含费 FIFO 归因({span};费:maker 2bps / taker 5bps 每边)")
    hdr = f"{'策略腿':<44}{'回合':>5}{'净$':>9}{'毛$':>9}{'费$':>7}{'胜率%':>6}{'最差$':>8}{'mkr/tkr':>10}"
    print(hdr)
    print("-" * len(hdr))
    tot_net = tot_gross = tot_fees = 0.0
    for leg in sorted(stat, key=lambda k: stat[k]["gross"] - stat[k]["fees"]):
        s = stat[leg]
        net = s["gross"] - s["fees"]
        tot_net += net
        tot_gross += s["gross"]
        tot_fees += s["fees"]
        wr = 100.0 * s["wins"] / s["rounds"] if s["rounds"] else 0.0
        name = f"{leg[0]}/{leg[1].split('-')[0]}"
        print(
            f"{name:<44}{s['rounds']:>5}{net:>9.2f}{s['gross']:>9.2f}"
            f"{s['fees']:>7.2f}{wr:>6.0f}{s['worst']:>8.2f}"
            f"{s['maker']:>5}/{s['taker']:<4}"
        )
    print("-" * len(hdr))
    print(f"{'合计':<44}{'':>5}{tot_net:>9.2f}{tot_gross:>9.2f}{tot_fees:>7.2f}")

    if args.open:
        print("\n当前未平持仓归属(FIFO 剩余 lots,名义按开仓价):")
        for leg in sorted(lots):
            dq = lots[leg]
            if not dq:
                continue
            ctv = ct_val(leg[1])
            net_q = sum(l[0] for l in dq) * ctv
            notion = sum(l[0] * l[1] for l in dq) * ctv
            if abs(net_q) < 1e-12:
                continue
            print(
                f"  {leg[0]}/{leg[1].split('-')[0]:<6} net={net_q:+.4f} 币"
                f"  名义≈${abs(notion):.0f}  lots={len(dq)}"
            )


if __name__ == "__main__":
    asyncio.run(main())
