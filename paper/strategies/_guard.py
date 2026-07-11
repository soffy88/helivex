"""事件处理器守护 — 单个策略的异常绝不允许终止整个引擎。

NautilusTrader 的 Data/Exec 引擎把 actor 处理器里任何未捕获异常视为致命
("System will terminate immediately"),一个策略的 bug 会砸崩节点内全部
策略,且重启会清零 INTERNAL 1H/1D bar 聚合(2026-07-10 settlement_price
事故:~30 轮崩溃重启,长周期策略假死 32 小时)。

所有 on_* 处理器必须套 @survive:异常只丢弃当前事件并记 ERROR,引擎存活。
"""

from __future__ import annotations

import functools
import traceback
from typing import Any, Callable


def survive(fn: Callable) -> Callable:
    """处理器级熔断:吞掉异常、记 ERROR、丢弃该事件,保住引擎。"""

    @functools.wraps(fn)
    def wrapper(self: Any, *args: Any, **kwargs: Any) -> Any:
        try:
            return fn(self, *args, **kwargs)
        except Exception as exc:
            try:
                self.log.error(
                    f"[guard] {type(self).__name__}.{fn.__name__} 异常已熔断: "
                    f"{exc!r} — 事件丢弃,引擎存活\n{traceback.format_exc()}"
                )
            except Exception:
                pass
            return None

    return wrapper


def close_positions_okx_safe(strategy: Any) -> None:
    """OKX-safe 的停机平仓,替代 on_stop 里的 close_all_positions()。

    close_all_positions 生成 NT 默认 clOrdId(O-20260711-…,含连字符),
    OKX 拒单 'Parameter clOrdId error' — 停机平仓从未真正成交,仓位悬在
    venue。按真实净仓自发 OKX-safe 市价单平掉。
    """
    from nautilus_trader.model.enums import OrderSide, TimeInForce
    from nautilus_trader.model.identifiers import InstrumentId
    from paper.order_ids import next_client_order_id

    inst_id = InstrumentId.from_str(strategy.config.instrument_id)
    try:
        net = float(strategy.portfolio.net_position(inst_id))
    except Exception as exc:
        strategy.log.error(f"[guard] close-on-stop 净仓查询失败: {exc!r}")
        return
    if net == 0:
        return
    instrument = strategy.cache.instrument(inst_id)
    if instrument is None:
        strategy.log.error("[guard] close-on-stop: instrument 不在 cache")
        return
    order = strategy.order_factory.market(
        instrument_id=inst_id,
        order_side=OrderSide.SELL if net > 0 else OrderSide.BUY,
        quantity=instrument.make_qty(abs(net)),
        time_in_force=TimeInForce.IOC,
        client_order_id=next_client_order_id(strategy._strategy_id()),
    )
    strategy.submit_order(order)
    strategy.log.info(f"[guard] close-on-stop: 平仓 net={net} → {order.side}")


def resync_position_from_venue(strategy: Any, kind: str) -> int:
    """订单被拒/未成交后,从 venue 真实净仓重同步 _position。

    _submit 在提交前乐观地置 _position=±1;若订单实际未成交,策略会带着
    幻影仓位运行,后续"平仓"单会在 venue 开出反向真实仓位。返回重同步
    后的方向(-1/0/1),调用方据此复位各自的辅助状态。
    """
    from nautilus_trader.model.identifiers import InstrumentId

    inst = strategy.config.instrument_id
    prev = strategy._position
    try:
        net = float(strategy.portfolio.net_position(InstrumentId.from_str(inst)))
    except Exception as exc:
        strategy.log.error(
            f"[guard] order {kind} 但仓位重同步失败: {exc!r} — 保留 _position={prev}"
        )
        return prev
    pos = 1 if net > 0 else (-1 if net < 0 else 0)
    strategy._position = pos
    strategy.log.error(
        f"[guard] order {kind} — _position {prev} -> {pos} (venue net={net})"
    )
    return pos
