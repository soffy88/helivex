"""风险定仓 — 仓位名义 = 风险预算 ÷ 止损距离百分比(turtle 式)。

qty_usd = risk_usd / (sl_dist / entry_px),再截断到 max_qty_usd(风控 cap 之下的
每笔名义上限)。效果:紧止损策略(5m ATR ~0.1-0.3%)必然顶到名义上限,宽止损策略
(1D 通道 3-5%)按风险缩仓 — 每笔美元风险归一,名义随止损宽度自适应。

risk_pct <= 0 或止损距离缺失 → 回退固定 fallback_qty_usd(向后兼容:未迁移/
rehydrate 无锚点的入场保持原 qty_usd 行为)。
"""

from __future__ import annotations

from paper.risk import BASE_EQUITY_USD


def risk_sized_qty_usd(
    risk_pct: float,
    entry_px: float,
    sl_dist: float | None,
    max_qty_usd: float,
    fallback_qty_usd: float,
) -> float:
    """入场名义美元。risk_pct 按 BASE_EQUITY_USD 的百分比计(1.0 = 每笔风险 1%)。"""
    if risk_pct <= 0 or not sl_dist or sl_dist <= 0 or entry_px <= 0:
        return fallback_qty_usd
    risk_usd = BASE_EQUITY_USD * risk_pct / 100.0
    return min(max_qty_usd, risk_usd * entry_px / sl_dist)
