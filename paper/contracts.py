"""OKX SWAP 合约面值(ctVal)单一真源。

`paper.fills.quantity` 存的是「张数」,1 张 = ctVal 个币。任何从 fills 反算
币/美元口径的地方(已实现/未实现 P&L、名义、NAV、回撤、净敞口)都必须乘 ctVal,
否则会把张数当币数——BTC(ctVal 0.01)P&L 放大 100 倍、ETH(0.1)10 倍、SOL(1)不变。

这是 paper/strategies/* 里 `instrument.multiplier`(NautilusTrader ctVal)的镜像,
供拿不到 NT instrument 对象的读取侧(gateway、paper/risk)复用。新增合约时在此更新。
"""

# instrument(不含 .VENUE 后缀)→ ctVal
CT_VAL: dict[str, float] = {
    "BTC-USDT-SWAP": 0.01,
    "ETH-USDT-SWAP": 0.1,
    "SOL-USDT-SWAP": 1.0,
}


def ct_val(instrument: str) -> float:
    """成交 instrument(形如 'BTC-USDT-SWAP.OKX')的 ctVal;现货/未知默认 1.0(数量即币数)。"""
    return CT_VAL.get(instrument.split(".")[0], 1.0)
