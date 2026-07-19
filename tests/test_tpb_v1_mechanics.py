"""TPB-001 runner 的机制单测 —— 用构造数据锁住三个最容易写错的地方。

这些是工程正确性验证(引擎是否按 spec §4 执行),不是策略验证:不碰真实行情、
不产生任何回测结论,因此不消耗 trial 预算、不违反"冻结后单次运行"的纪律。

锁住的机制:
  E5  成交判定必须严格 —— low < 限价才算成交,low == 限价(仅 touch)不算
  X6  同 bar 内 SL 优先于 TP(最坏假设)
  X3  吊灯止损单调 —— 多头只升不降
"""

from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ops.scripts.tpb_v1_runner import (  # noqa: E402
    INSTRUMENTS,
    PortfolioSim,
    TPBConfig,
)

INST = "BTC-USDT-SWAP"
T0 = dt.datetime(2025, 1, 1, tzinfo=dt.timezone.utc)


def _bars(rows: list[dict]) -> pd.DataFrame:
    """构造 1h 表。默认让 regime 为多头开启(close4 远高于云顶、adx4=40)。"""
    out = []
    for i, r in enumerate(rows):
        base = {
            "open": r.get("close", 100.0),
            "high": r.get("high", 101.0),
            "low": r.get("low", 99.0),
            "close": r.get("close", 100.0),
            "volume": 1.0,
            "kijun": r.get("kijun", 100.0),
            "atr": r.get("atr", 1.0),
            "cloud_top": r.get("cloud_top", 90.0),
            "cloud_bot": r.get("cloud_bot", 80.0),
            "adx4": r.get("adx4", 40.0),
            "close4": r.get("close4", 120.0),
            "funding": r.get("funding", float("nan")),
        }
        base.update({k: v for k, v in r.items() if k in base})
        out.append((T0 + dt.timedelta(hours=i), base))
    idx = pd.DatetimeIndex([t for t, _ in out])
    return pd.DataFrame([b for _, b in out], index=idx)


def _sim(rows: list[dict], cfg: TPBConfig | None = None) -> PortfolioSim:
    df = _bars(rows)
    empty = df.iloc[0:0]
    data = {i: (df if i == INST else empty) for i in INSTRUMENTS}
    s = PortfolioSim(cfg or TPBConfig(), data)
    s.run()
    return s


def test_e5_touch_does_not_fill():
    """low == 限价 只是 touch,不算成交;low < 限价 才成交。"""
    # bar0 收盘挂单于 kijun=100(close 101 > kijun)
    # bar1 low 正好 100 → 不成交,计入 touch_rejects
    # bar2 low 99.5 < 100 → 成交
    s = _sim(
        [
            {"close": 101.0, "high": 102.0, "low": 100.5, "kijun": 100.0},
            {"close": 101.0, "high": 102.0, "low": 100.0, "kijun": 100.0},  # touch
            {"close": 101.0, "high": 102.0, "low": 99.5, "kijun": 100.0},  # fill
            {"close": 101.0, "high": 102.0, "low": 100.5, "kijun": 100.0},
        ]
    )
    assert s.touch_rejects == 1, "low==限价 必须被判为未成交并计数"
    assert len(s.trades) >= 1 or s.pos[INST] is not None, "low<限价 必须成交"


def test_x6_stop_beats_tp_same_bar():
    """同一根 bar 内既穿 TP 又穿 SL 时,必须按 SL 结算(最坏假设)。"""
    # ATR=1, sl_atr=2 → 止损 = entry-2;TP = entry + 2R = entry+4
    # bar2 成交于 100;bar3 同时 high=110(过TP=104)且 low=97(过SL=98)
    s = _sim(
        [
            {"close": 101.0, "high": 102.0, "low": 100.5, "kijun": 100.0, "atr": 1.0},
            {
                "close": 101.0,
                "high": 102.0,
                "low": 99.0,
                "kijun": 100.0,
                "atr": 1.0,
            },  # fill@100
            {
                "close": 100.0,
                "high": 110.0,
                "low": 97.0,
                "kijun": 100.0,
                "atr": 1.0,
            },  # 双穿
            {"close": 100.0, "high": 101.0, "low": 99.5, "kijun": 100.0, "atr": 1.0},
        ]
    )
    assert s.trades, "应当已平仓"
    t = s.trades[0]
    assert t.exit_reason.startswith("stop"), f"同bar双穿必须走止损,实际={t.exit_reason}"
    assert not t.tp_filled, "同bar双穿时不应记为 TP 已成交"


def test_x3_trailing_stop_monotonic_long():
    """多头吊灯止损只升不降 —— 价格回落时有效止损不得跟着下移。"""
    rows = [
        {"close": 101.0, "high": 102.0, "low": 100.5, "kijun": 100.0, "atr": 1.0},
        {
            "close": 101.0,
            "high": 102.0,
            "low": 99.0,
            "kijun": 100.0,
            "atr": 1.0,
        },  # fill@100
    ]
    # 价格拉到 130 再回落到 105,吊灯(3×ATR)应停在高点算出的位置不回落
    for c in (110.0, 120.0, 130.0, 120.0, 110.0, 105.0):
        rows.append(
            {"close": c, "high": c + 1, "low": c - 1, "kijun": 100.0, "atr": 1.0}
        )

    df = _bars(rows)
    empty = df.iloc[0:0]
    data = {i: (df if i == INST else empty) for i in INSTRUMENTS}
    s = PortfolioSim(TPBConfig(), data)

    stops: list[float] = []
    for ts in df.index:
        row = df.loc[ts]
        s._roll_day(ts)
        s._manage(INST, ts, row)
        s._try_fill(INST, ts, row)
        s._place(INST, ts, row)
        if s.pos[INST] is not None:
            stops.append(s.pos[INST].eff_stop)

    assert len(stops) >= 4, "样本不足以检验单调性"
    for a, b in zip(stops, stops[1:]):
        assert b >= a - 1e-9, f"多头有效止损出现下移: {a} → {b}"
    assert max(stops) > stops[0], "价格上行后吊灯应当抬升过"


def test_no_lookahead_fill_uses_next_bar():
    """限价在 bar t 收盘挂出,成交只能发生在 t+1 及以后,不能在 t 当根。"""
    # bar0 的 low 就低于 kijun,但此时还没有挂单 → 不应成交
    s = _sim(
        [
            {"close": 101.0, "high": 102.0, "low": 95.0, "kijun": 100.0},
            {"close": 101.0, "high": 102.0, "low": 100.5, "kijun": 100.0},
        ]
    )
    assert not s.trades and s.pos[INST] is None, "挂单当根不得成交(前视)"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
