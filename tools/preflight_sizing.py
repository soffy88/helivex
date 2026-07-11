"""开仓链路预检 — 用 OKX 实时合约规格实测每个策略腿的算量。

每条腿跑与策略 _submit 完全相同的数学:
  raw = qty_usd / (ctVal × last_price)   # SWAP:美元名义 → 张数
  qty = floor(raw / lotSz) * lotSz       # make_qty 的向下取整语义
  qty < minSz → 回退 minSz(与策略代码兜底一致)

任何一条腿算量为 0 或抛错 = FAIL,退出码非零。
用法(容器内,走 7890 代理):python tools/preflight_sizing.py
"""

from __future__ import annotations

import json
import math
import os
import sys
import urllib.request

PROXY = os.environ.get("OKX_PROXY", "http://127.0.0.1:7890")

# 策略腿:(策略, 标的, qty_usd) — 与 paper/node.py 配置对齐
LEGS = [
    ("donchian_4h", "BTC-USDT-SWAP", 200.0),
    ("donchian_4h", "ETH-USDT-SWAP", 200.0),
    ("donchian_4h", "SOL-USDT-SWAP", 200.0),
    ("vwap_mr_1h", "SOL-USDT-SWAP", 200.0),  # min_quantity 下单,列出仅供对照
    ("scalp_5m", "BTC-USDT-SWAP", 50.0),  # min_quantity 下单
    ("scalp_5m", "ETH-USDT-SWAP", 50.0),
    ("scalp_5m", "SOL-USDT-SWAP", 50.0),
    ("trend_follower_port", "BTC-USDT-SWAP", 200.0),
    ("trend_follower_port", "ETH-USDT-SWAP", 200.0),
    ("trend_follower_port", "SOL-USDT-SWAP", 200.0),
    ("scalper_v2_port", "BTC-USDT-SWAP", 50.0),
    ("scalper_v2_port", "ETH-USDT-SWAP", 50.0),
    ("scalper_v2_port", "SOL-USDT-SWAP", 50.0),
    ("futures_signal_port", "BTC-USDT-SWAP", 100.0),
    ("futures_signal_port", "ETH-USDT-SWAP", 100.0),
    ("futures_signal_port", "SOL-USDT-SWAP", 100.0),
]

# min_quantity 固定下单的策略(不走 qty_usd 换算,永不归零)
MIN_QTY_STRATS = {"vwap_mr_1h", "scalp_5m"}


def _get(url: str) -> dict:
    handler = urllib.request.ProxyHandler({"http": PROXY, "https": PROXY})
    opener = urllib.request.build_opener(handler)
    req = urllib.request.Request(
        url, headers={"User-Agent": "Mozilla/5.0 helivex-preflight"}
    )
    with opener.open(req, timeout=15) as r:
        return json.loads(r.read())


def main() -> int:
    specs = {
        d["instId"]: d
        for d in _get("https://www.okx.com/api/v5/public/instruments?instType=SWAP")[
            "data"
        ]
    }
    ticks = {
        d["instId"]: float(d["last"])
        for d in _get("https://www.okx.com/api/v5/market/tickers?instType=SWAP")["data"]
    }
    rows, failed = [], 0
    for strat, inst, qty_usd in LEGS:
        spec, px = specs.get(inst), ticks.get(inst)
        if not spec or not px:
            rows.append((strat, inst, "-", "-", "FAIL 无合约规格/行情"))
            failed += 1
            continue
        ct_val, lot, min_sz = (
            float(spec["ctVal"]),
            float(spec["lotSz"]),
            float(spec["minSz"]),
        )
        if strat in MIN_QTY_STRATS:
            rows.append(
                (
                    strat,
                    inst,
                    f"{min_sz}张(min)",
                    f"${min_sz * ct_val * px:.2f}",
                    "PASS(min_qty 模式)",
                )
            )
            continue
        raw = qty_usd / (ct_val * px)
        qty = math.floor(raw / lot + 1e-9) * lot
        note = ""
        if qty < min_sz:
            qty, note = min_sz, "(回退 minSz)"
        notional = qty * ct_val * px
        ok = qty > 0
        rows.append(
            (
                strat,
                inst,
                f"{qty:g}张{note}",
                f"${notional:.2f}",
                "PASS" if ok else "FAIL 算量为零",
            )
        )
        if not ok:
            failed += 1
    w = [max(len(str(r[i])) for r in rows) for i in range(5)]
    for r in rows:
        print("  ".join(str(c).ljust(w[i]) for i, c in enumerate(r)))
    print(
        f"\n{'FAIL: ' + str(failed) + ' 条腿算量断裂' if failed else 'ALL PASS — ' + str(len(rows)) + ' 条腿算量全部有效'}"
    )
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
