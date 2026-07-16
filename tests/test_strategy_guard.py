"""_guard 单元验证:处理器异常熔断 + 拒单后仓位重同步。

survive 部分不依赖 nautilus_trader,宿主 venv 可跑;resync 的 venue 交互
用鸭子类型伪对象覆盖(真实 InstrumentId 解析在容器内集成验证)。
"""

from paper.strategies._guard import survive


class _Log:
    def __init__(self):
        self.errors = []

    def error(self, msg):
        self.errors.append(msg)


class _Dummy:
    def __init__(self):
        self.log = _Log()

    @survive
    def on_bar(self, bar):
        raise RuntimeError("boom")

    @survive
    def on_order_filled(self, event):
        return "handled"


def test_survive_swallows_exception_and_logs():
    d = _Dummy()
    assert d.on_bar(object()) is None  # 不向引擎抛出
    assert any("on_bar" in e and "boom" in e for e in d.log.errors)


def test_survive_passthrough_on_success():
    d = _Dummy()
    assert d.on_order_filled(object()) == "handled"
    assert d.log.errors == []


def test_survive_survives_broken_logger():
    class _NoLog:
        @survive
        def on_bar(self, bar):
            raise RuntimeError("boom")

    assert _NoLog().on_bar(object()) is None  # log 缺失也不炸
