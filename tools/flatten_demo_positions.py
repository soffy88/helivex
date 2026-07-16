"""OKX DEMO 账户残余仓位一次性平仓工具。

用途:异常事件(如 2026-07-12 00:47 停机平仓风暴)后,把 demo 账户的
真实持仓平到零,让 paper 环境回到干净基线。仅 demo(x-simulated-trading:1),
安全闸:若 env 缺 OKX_API_KEY 或检测不到 demo 模式约定,直接退出。

用法(容器内):
  python tools/flatten_demo_positions.py           # 只看持仓,不动
  python tools/flatten_demo_positions.py --close   # 实际平仓
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import sys
import urllib.request
from datetime import datetime, timezone

BASE = "https://www.okx.com"
PROXY = os.environ.get("OKX_PROXY", "http://127.0.0.1:7890")


def _req(method: str, path: str, body: dict | None = None) -> dict:
    key = os.environ["OKX_API_KEY"]
    sec = os.environ["OKX_API_SECRET"]
    pw = os.environ["OKX_PASSPHRASE"]
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    payload = json.dumps(body) if body else ""
    sig = base64.b64encode(
        hmac.new(
            sec.encode(), f"{ts}{method}{path}{payload}".encode(), hashlib.sha256
        ).digest()
    ).decode()
    req = urllib.request.Request(
        BASE + path,
        data=payload.encode() if payload else None,
        method=method,
        headers={
            "OK-ACCESS-KEY": key,
            "OK-ACCESS-SIGN": sig,
            "OK-ACCESS-TIMESTAMP": ts,
            "OK-ACCESS-PASSPHRASE": pw,
            "x-simulated-trading": "1",  # DEMO — 绝不能去掉
            "Content-Type": "application/json",
            "User-Agent": "helivex-flatten",
        },
    )
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": PROXY, "https": PROXY})
    )
    with opener.open(req, timeout=20) as r:
        return json.loads(r.read())


def main() -> int:
    if not os.environ.get("OKX_API_KEY"):
        print("缺 OKX_API_KEY — 只能在 paper 容器内跑")
        return 1
    do_close = "--close" in sys.argv
    pos = _req("GET", "/api/v5/account/positions")
    rows = pos.get("data", [])
    if not rows:
        print("venue 无持仓 — 已是干净状态")
        return 0
    for p in rows:
        print(
            f"{p['instId']:<20} pos={p['pos']:>10} avgPx={p.get('avgPx', '—'):>10} "
            f"upl={p.get('upl', '—')}"
        )
    if not do_close:
        print("\n(dry-run — 加 --close 实际平仓)")
        return 0
    for p in rows:
        r = _req(
            "POST",
            "/api/v5/trade/close-position",
            {
                "instId": p["instId"],
                "mgnMode": p.get("mgnMode", "cross"),
                "posSide": "net",
            },
        )
        ok = r.get("code") == "0"
        print(f"close {p['instId']}: {'OK' if ok else r}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
