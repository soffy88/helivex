# Veya Loop × Helivex 量化交易适配层（研究态）
#
# 闭环：参数搜索（Veya Loop BO）→ 回测评价（Helivex 引擎）→ 风控门禁 → 可审计决策。
# 原则：Helivex 负责数据与撮合/回测真相；Veya Loop 负责搜索、效用、停损策略与审计。
# 本阶段**不**重写回测引擎、**不**直连实盘下单（只出「建议参数」）。

## 目录结构

```text
integrations/veya_loop_quant/
  helivex_adapter/
    __init__.py
    discovery_types.py      # 协议 dataclass（与 Helivex 解耦）
    backtest_bridge.py      # ⚠️ 唯一 import Helivex 内部 API 的文件
    metrics.py              # Sharpe/MDD 兜底计算（引擎缺字段时）
    risk_gate.py            # utility = Sharpe − MDD − 换手；RiskLimits 门禁
    optimize_loop.py        # bayesian_optimize 主入口 + train/test 门禁
    audit_log.py            # JSONL 审计
    cli.py                  # YAML 配置驱动的一次真实优化
    strategies_port.py      # 现有 strategies/*.yaml 的引擎侧信号移植（6 个策略）
    data_tools.py           # TimescaleDB → OHLCV parquet（任意周期聚合）
  configs/example_search_space.yaml
  tests/
    test_adapter_contract.py
    test_optimize_smoke.py
  run_scan.py               # 6 策略批量扫描（见 docs/integration/VEYA_LOOP_UTILITY_SCAN.md）
```

## 安装（helivex venv，Python 3.14）

```bash
cd /data/soffy/projects/helivex
source venv/bin/activate
pip install -r integrations/veya_loop_quant/requirements-veya-loop.txt
# 已装：helios_backtest_v51 / helivex_signal / helivex_regime / helivex_oskill（editable）
# veya-loop 由 _assembly 自动装配 /data/soffy/projects/veya/platform/3O 主库
```

## 快速开始（真实 Helivex）

```bash
# 1) 生成真实数据（DB → 特征 → regime → funding → parquet）
python -m helivex.scripts.run_features --symbol BTCUSDT.P --timeframe 1h \
    --start 2026-05-01 --end 2026-08-01 --out data/btc_2026q2_funded_1h.parquet

# 2) BO 搜参（train 2026-05→06，test 2026-07 门禁）→ 审计 JSONL
python -m helivex_adapter.cli \
    --config integrations/veya_loop_quant/configs/example_search_space.yaml \
    --audit integrations/veya_loop_quant/audit/optimize.jsonl
```

## 契约

```python
from helivex_adapter.discovery_types import BacktestRequest
from helivex_adapter.backtest_bridge import run_backtest   # 唯一 Helivex 入口
from helivex_adapter.optimize_loop import optimize_strategy_params

m = run_backtest(BacktestRequest(strategy_id="directional", params={...},
                                 symbols=["BTC-USDT-SWAP"], start="...", end="...",
                                 timeframe="1h", extra={"parquet_path": "..."}))
res = optimize_strategy_params(strategy_id="directional", symbols=[...],
                               start=..., end=..., search_space={...},
                               test_window=(...), ...)
```

## 与 Veya Loop 能力映射（量化语义）

| Veya Loop | 本层场景 |
|-----------|----------|
| `bayesian_optimize` | 策略超参搜索（替代穷举网格） |
| `utility / λ·cost` | `Sharpe − MDD − 换手`（`risk_gate.utility`） |
| `risk_gate` | 样本内最优仍可能拒收（过拟合/交易过少） |
| 反事实（可延后） | 成本/体制压力推演 |
| CapabilityToken（可延后） | 仅当接仿真/实盘下单时启用 |

## 现有策略移植

`strategies_port.py` 把 `strategies/*.yaml` 的 6 个 paper 策略（trend_dual / trend_follower_port /
vwap_mr_1h / futures_signal_port / scalper_v2_port / scalp_5m）翻译成引擎
`StrategyInterface` 信号适配器，复用引擎撮合/成本/CPCV。批量扫描与实用性评估见
`docs/integration/VEYA_LOOP_UTILITY_SCAN.md`（6/6 REJECT，与仓库 R4/R5 判决一致）。

## 防过拟合（硬性）

1. 优化只发生在训练区间 `[start, end)`；
2. 提供 `test_window` 后，best 在测试区间复评，`accepted` 以测试集门禁为准
   （`gate_on="auto"`；可显式 `gate_on="train"`）；
3. `n_init+n_iter` 默认偏小（回测 ~0.5 s/次 → 20 次 ≈ 10 s）；
4. 禁止用测试集调 `search_space` 边界。

## 审计

每次 `optimize_strategy_params` 由调用方写一行 JSONL（`audit_log.log_optimize`）：
`ts / best_params / utility / accepted / gate_reason / metrics(train) / test_metrics / meta`。

## 风险与边界

- 研究态：不直连 OKX 实盘/仿真；实盘必须另接权限令牌 + 人工/风控确认。
- `DirectionalStrategy.config_space()` 驱动 DSR 通缩；BO 场景请经 `extra["config_grid"]`
  声明真实搜索广度（YAML `backtest.config_grid`）。
- 引擎确定性（纯逐 bar 模拟），无随机噪声；结果差异仅来自数据切片与参数。
