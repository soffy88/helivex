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


_EXPOSURE_SYNC_STARTED = False


def start_exposure_sync(strategy: Any, interval: float = 60.0) -> None:
    """全局唯一的敞口对账循环(所有策略 on_start 都调,首个生效)。

    RISK 账本是进程内存,重启即清零,而 venue 持仓仍在 — cap 会在低估
    敞口的状态下放行新开仓。每 interval 秒把 cache 全量持仓的美元敞口
    (qty×ctVal×开仓均价)与账本对账,差额记 ("external", inst) 键。
    """
    global _EXPOSURE_SYNC_STARTED
    if _EXPOSURE_SYNC_STARTED:
        return
    _EXPOSURE_SYNC_STARTED = True
    import asyncio

    from paper.risk import RISK

    async def _loop() -> None:
        await asyncio.sleep(20)  # 等启动对账落定
        while True:
            try:
                by_inst: dict[str, float] = {}
                for p in strategy.cache.positions_open():
                    inst = strategy.cache.instrument(p.instrument_id)
                    ct = float(inst.multiplier) if inst is not None else 1.0
                    key = str(p.instrument_id)
                    by_inst[key] = by_inst.get(key, 0.0) + abs(
                        float(p.signed_qty)
                    ) * ct * float(p.avg_px_open)
                RISK.sync_external_exposure(by_inst)
            except Exception as exc:
                try:
                    strategy.log.warning(f"[guard] 敞口对账失败: {exc!r}")
                except Exception:
                    pass
            await asyncio.sleep(interval)

    asyncio.ensure_future(_loop())


def own_open_qty(strategy: Any) -> float | None:
    """本策略在其标的上的实际净持仓(张,含符号);无仓/查询失败返回 None。

    平仓单必须用这个数量而不是按当前价重算 — 价格动了重算数量就和开仓
    数量不等,每回合留反向零头(实证:scalper_v2_sol 残 -0.01 张)。
    重启后的 EXTERNAL 仓位不带 strategy_id → 返回 None,调用方走原逻辑。
    """
    from nautilus_trader.model.identifiers import InstrumentId

    try:
        ps = strategy.cache.positions_open(
            instrument_id=InstrumentId.from_str(strategy.config.instrument_id),
            strategy_id=strategy.id,
        )
        net = float(sum(p.signed_qty for p in ps))
        return net if net != 0 else None
    except Exception:
        return None


def close_positions_okx_safe(strategy: Any) -> None:
    """OKX-safe 的停机平仓,替代 on_stop 里的 close_all_positions()。

    close_all_positions 生成 NT 默认 clOrdId(O-20260711-…,含连字符),
    OKX 拒单 'Parameter clOrdId error' — 停机平仓从未真正成交,仓位悬在
    venue。改为自发 OKX-safe 市价单平掉。

    ⚠ 只平【本策略自己】的持仓(cache 按 strategy_id 过滤),绝不能用
    portfolio.net_position — 那是账户级净仓,BTC/ETH/SOL 各被 4-6 个
    策略实例共享,每个实例都平一次同一净仓 = 反向放大数倍。
    2026-07-12 00:47 断连自愈重启时该 bug 实爆:6 实例 × SELL 16.34 +
    6 × BUY 8.31,账户 USDT 4998→933。
    """
    from nautilus_trader.model.enums import OrderSide, TimeInForce
    from nautilus_trader.model.identifiers import InstrumentId
    from paper.order_ids import next_client_order_id

    inst_id = InstrumentId.from_str(strategy.config.instrument_id)
    try:
        own = strategy.cache.positions_open(
            instrument_id=inst_id, strategy_id=strategy.id
        )
        net = float(sum(p.signed_qty for p in own))
    except Exception as exc:
        strategy.log.error(f"[guard] close-on-stop 本策略持仓查询失败: {exc!r}")
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
    strategy.log.info(
        f"[guard] close-on-stop: 平本策略仓位 net={net}({len(own)} pos) → {order.side}"
    )


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
