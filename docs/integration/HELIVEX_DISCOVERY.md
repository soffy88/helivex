# HELIVEX DISCOVERY — 回测引擎与数据层勘察

> CC 第一步输出（`HELIVEX_QUANT_VEYA_LOOP_CC_SPEC.md` §1）。
> 结论驱动 `integrations/veya_loop_quant/helivex_adapter/backtest_bridge.py` 的实现。
> 勘察时间：2026-08（仓库当前状态）。

## 0. 一句话结论

Helivex = **NautilusTrader 系量化系统**（README），但本阶段回测真相由 **3O 主库
`helios_backtest_v51`**（`platform/3O`，editable 安装）提供；数据层 = **TimescaleDB
（OKX 5m）→ `run_features` 特征/regime/funding 管线 → parquet**。Veya Loop 适配层
只读 parquet + 调引擎 API，不触碰撮合/订单。

## 1. 包与入口

| 项 | 值 |
|----|----|
| 仓库根 | `/data/soffy/projects/helivex`（本项目层：strategy YAML / 脚本 / ops / web） |
| 回测引擎 | `helios_backtest_v51`（editable 安装，源码 `/data/soffy/projects/platform/3O/helios_backtest_v51/src`） |
| 信号策略 | `helivex_signal`（editable 安装，源码 `/data/soffy/projects/platform/3O/btc_signal/src`），`DirectionalStrategy` 原生满足 `StrategyInterface`（`fingerprint/reset/on_bar/config_space`） |
| 特征/regime | `helivex_oskill` + `helivex_regime`（editable，`platform/3O/btc_oskill|btc_regime`） |
| 数据获取 | TimescaleDB `postgresql://helios:helios_dev_pass@localhost:5434/helivex` → `market_data.ohlcv_5m`（OKX BTC-USDT-SWAP 5m，2019→2026）与 `market_data.binance_funding_history` |
| 驱动脚本 | `helivex/scripts/run_features.py`（DB→parquet）、`helivex/scripts/run_backtest_v51.py`（引擎调用示例） |

## 2. 回测入口（DISCOVERY 核心答案）

### 引擎 API（`helios_backtest_v51`）

```python
from helios_backtest_v51 import (
    BacktestConfig, CostModel, BarRow, ValidationReport, run_validation,
)
from helios_backtest_v51.cpcv import CPCVConfig
from helios_backtest_v51.simulator import simulate, SimResult

run_validation(strategy: StrategyInterface, bars: list[BarRow], *, config: BacktestConfig) -> ValidationReport
simulate(strategy, bars, *, cost_model: CostModel) -> SimResult   # 全窗口 equity/trades

@dataclass(frozen=True)
class BacktestConfig:
    cpcv: CPCVConfig = CPCVConfig(n_groups=10, n_test_groups=2, embargo_bars=0, purge_overlap=True)
    cost_model: CostModel = CostModel(taker_fee_bps=5.0, maker_fee_bps=2.0,
                                      assume_taker_fraction=1.0, fixed_slippage_bps=1.0)
    dsr_promote_threshold: float = 0.95
    pbo_promote_threshold: float = 0.50
    sr_star_baseline: float = 0.0
```

### 出参字段（映射到 BacktestMetrics）

| ValidationReport 字段 | 含义 | 适配器用法 |
|---|---|---|
| `sharpe_median` | CPCV 全部 OOS fold 的 **per-bar Sharpe 中位数**（稳定 Sharpe 字段 ✓） | → `metrics.sharpe` |
| `per_path_max_drawdown: list[float]` | 每个 OOS fold 的 equity 最大回撤（正数，0.15=15%） | → `metrics.max_drawdown = max(per_path)`（最差 fold） |
| `verdict / verdict_reason` | `PROMOTE/REJECT/INCONCLUSIVE` + 原因（DSR/PBO/SR） | → `metrics.raw["verdict_*"]` |
| `psr / deflated_sharpe_ratio / pbo / n_trials` | PSR / DSR / PBO / 声明搜索规模 | → `metrics.raw`（审计用） |
| **缺失**：total_return、n_trades、turnover、equity curve | ValidationReport 不带全窗口曲线 | 用 `simulate()` 补（引擎 API，非重写） |

`SimResult`：`equity_curve: list[float]`、`per_bar_returns`、`trades: list[TradeRecord]`
（每条含 `prev_fraction/new_fraction/cost`）、`total_costs`、`total_funding`。

- **turnover**：`Σ|new_fraction − prev_fraction|`（one-way），年化 = `sum * 252 / n_bars`。
- **cost_drag**：`total_costs`（初始 equity=1.0，故为相对资本比例）。
- **n_trades**：`len(trades)`。
- **total_return**：全窗口 `equity_curve[-1]/equity_curve[0] − 1`。

### 策略侧 API

```python
SignalConfig.for_timeframe("1h" | "15m") -> SignalConfig   # frozen dataclass，可 dataclasses.replace 覆写参数
DirectionalStrategy(cfg, config_grid=...)                   # config_grid 声明搜索广度 → n_trials → DSR 通缩
```

可搜参数（`SignalConfig` 字段，注意 int/float 类型）：`mom_donchian_lookback`(int)、
`mom_ema_fast/slow`(int)、`mom_atr_lookback`(int)、`mom_trail_atr_k`(float)、
`mom_funding_extreme_block`(float)、`mom_max_pyramid_tranches`(int)、
`mom_pyramid_min_profit_pct`(float)、`mr_zscore_lookback`(int)、`mr_zscore_entry/exit`(float)、
`mr_atr_lookback`(int)、`mr_stop_atr_k`(float)、`mr_time_stop_bars`(int)、`mr_funding_confirm`(float)、
`kelly_fraction`(float)、`max_position_fraction`(float)、`max_gross_leverage`(float)、
`vol_target_annual`(float)、`tail_index_ref`(float)、`tail_index_floor_mult`(float)、
`min_edge_to_trade`(float)、`edge_ewma_halflife_bars`(int)、`edge_cold_start`(float)、
`edge_warmup_trade_count`(int)。

⚠️ 规格示例里的 `lookback/threshold/lot_pct` 为占位参数名；真实实现映射到上述 `SignalConfig` 字段。

### 入参（数据层）

- 输入：**parquet 文件路径**（`run_features` 产物），列：`bar_close_ts, open, high, low, close,
  log_return, realized_var, tail_index, hurst_h2, hurst_r2, delta_alpha, regime,
  risk_multiplier, funding_rate`。
- `load_bars(parquet) -> list[BarRow]`（`helivex/scripts/run_backtest_v51.py` 提供，适配器复用）。
- 时间区间过滤：按 `bar_close_ts ∈ [start, end)` 切片（适配器内做，数据读取仍用 Helivex 函数）。

## 3. 耗时量级

| 场景 | 耗时 |
|---|---|
| 1,660 根 1h bar（≈2.3 个月），CPCV C(10,2) + 全窗口 simulate | **≈0.5 s**（本机） |
| run_features：5m→1h→特征→regime→funding，1 年数据（≈8,700 bar，需 500 bar Hurst 预热） | 分钟级（DB IO + mfdfa 滚动窗口） |

→ 本适配器默认 `n_init+n_iter ≤ 20`（规格 §6.3），一次真实优化（BO 20 次回测）≈ 10–15 s。

## 4. Python 版本与依赖安装

- Python：**3.14**（`venv/`，虚拟环境原先无 pip，已用 `ensurepip` 引导）。
- 依赖（editable 安装，无网络要求时用本地源）：
  - `helios_backtest_v51`、`helivex_signal`、`helivex_regime`、`helivex_oskill`（`platform/3O/btc_*`）；
  - `veya-loop`（`/data/soffy/projects/veya/veya_loop`，editable；其 `_assembly` 自动装配
    `/data/soffy/projects/veya/platform/3O/*` 主库，含 `oprim._bayes_opt_plan`）；
  - 运行时补装：`pyarrow`、`tree-sitter`、`tree-sitter-python`（veya oprim `__init__` 的 heavy-SDK 依赖）。
- 回测引擎依赖 pandas 3.0.3 / numpy 2.4.6 / scipy / scikit-learn，均已在 venv。

## 5. 与规格的差异与处置

| 规格假设 | 实际 | 处置 |
|---|---|---|
| `bayesian_optimize` 返回对象（`bo.best_x`） | 返回 **dict**：`{best_x, best_y, xs, ys, n_calls}`，默认 `minimize=True` | 适配层兼容两者（`getattr` 回退 `[]`），maximize 时传 `minimize=False` |
| 回测出参含 total_return/n_trades/turnover | ValidationReport 只给 Sharpe/MDD/DSR/PBO | bridge 内追加一次全窗口 `simulate()` 补齐（仍全部走引擎 API） |
| 策略 ID + 参数 dict 入参 | 真实入口是 `SignalConfig` frozen dataclass + `DirectionalStrategy` | `strategy_id="directional"` → `SignalConfig.for_timeframe(timeframe)` → `dataclasses.replace` 覆写参数 |
| 数据 API | 无独立数据 API；DB 管线 → parquet → `load_bars` | bridge 支持 `extra["parquet_path"]` / `extra["bars"]` / `data/` 自动查找 |

## 6. 风险与边界（本阶段）

- 本阶段**研究态**：只读 parquet + 引擎调用；不连 OKX 实盘/仿真（paper 层在 `paper/`，独立）。
- `DirectionalStrategy.config_space()`（默认 12 组合）驱动 DSR 通缩；BO 搜连续参数时由调用方
  通过 `extra["config_grid"]` 声明真实搜索广度，避免 DSR 失真。
- 1h 数据需 ≥500 bar 预热（Hurst 窗口），短区间回测会被 `run_features` 拒绝。
- 引擎确定性：`simulate`/`run_validation` 为纯 Python 逐 bar，无随机性 → BO 噪声仅来自数据切片。
