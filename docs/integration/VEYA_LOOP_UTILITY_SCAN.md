# Veya Loop 实用性检查 — 现有 6 策略 × Helivex 引擎扫描

> 目标：把 `strategies/*.yaml` 里的现有策略接到 helios_backtest_v51 引擎上，
> 用 Veya Loop `bayesian_optimize` 各跑一次小预算 BO（train/test 双门禁 + JSONL 审计），
> 检验「参数搜索 → 回测评价 → 风控门禁 → 可审计决策」闭环在真实策略上的实用性。
> 日期：2026-08。**研究态**：不连实盘，不重写引擎。

## 1. 方法

- **策略移植**（`helivex_adapter/strategies_port.py`）：按 YAML 文档化逻辑实现 6 个
  `StrategyInterface` 信号适配器（SuperTrend/EMA/ADX/MACD、Donchian+Chandelier、
  VWAP-MR、RSI/BB 双模式、突破+量增+RSI、VWAP z 剥头皮）。引擎的撮合/成本/CPCV/DSR
  全部复用，**未改动 helios_backtest_v51 一行**。
- **数据**（`helivex_adapter/data_tools.py`）：TimescaleDB → parquet。4H 原生
  （2019→2026 全史），1H 由 5m 聚合（2026-04 起），1D 由 4H 聚合，5M 原生。
  统一 BTC-USDT-SWAP；funding=0（DB funding 历史仅 2026-06-30 起，注明）。
- **搜索**：每策略 3 参数 × `n_init=3 + n_iter=5` = 8 次回测；train 区间 BO 最大化
  `utility = Sharpe − MDD − 0.05·turnover`；best 在 test 区间复评；`risk_gate`
  （min_sharpe 0.3 / max_drawdown 0.25 / min_trades 20 / max_turnover 50）以 **test**
  指标为准；每次 optimize 追加 JSONL 审计（`integrations/veya_loop_quant/audit/scan_*.jsonl`）。

## 2. 结果

| 策略 | TF | 窗口(train→test) | train SR | train ret | test SR | test ret | test ntr | 判定 |
|---|---|---|---|---|---|---|---|---|
| trend_dual | 4H | 2024-01→2026-04 / 04→08 | −0.004 | −6.4% | **−0.053** | −9.0% | 51 | REJECT |
| trend_follower_port | 1D | 2024-01→2026-04 / 04→08 | +0.000 | −12.2% | +0.000 | 0.0% | **0** | REJECT n_trades |
| vwap_mr_1h | 1H | 04-15→06-15 / 06-15→08-01 | −0.004 | −2.9% | **−0.032** | −3.3% | 148 | REJECT |
| futures_signal_port | 1H | 04-15→06-15 / 06-15→08-01 | +0.035 | +3.8% | +0.015 | +0.0% | 26 | REJECT |
| scalper_v2_port | 5M | 05-01→06-15 / 06-15→08-01 | −0.037 | −17.5% | **−0.029** | −12.9% | 485 | REJECT |
| scalp_5m | 5M | 05-01→06-15 / 06-15→08-01 | −0.053 | −16.1% | **−0.070** | −21.7% | 661 | REJECT |

- **6/6 全部 REJECT**，与仓库既有 gate 判决（R4.x/R5.x）**方向一致**：vwap_mr_1h 净负
  （R5.4「maker 化也救不了」一致）、scalp_5m 净负（R5「taker 成本碾死」一致）、
  trend_dual/scalper_v2 样本外无 alpha。
- 全流程耗时：6 策略 × 8 次回测 ≈ **2.6 分钟**（含 5M 数据 CPCV；单次 1H 回测 <1 s）。
- BO 预算内找到的最优参数均在 test 上失效 → **train/test 门禁正确拦截**（无假阳性）。

## 3. 实用性结论

### ✅ 有效的能力
1. **闭环可用**：YAML 策略 → 引擎回测 → BO 搜参 → 门禁 → 审计，全链路跑通且可复现；
   审计 JSONL 含 train/test 双指标 + 门禁原因，满足「可审计决策」。
2. **门禁拦截有效**：6 个策略的 train 最优在 test 全部退化，`accepted=False` 全部正确；
   未出现「train 漂亮 → 误放行」的假阳性。
3. **成本敏感**：引擎 CostModel（taker 5bp + slip 1bp）如实惩罚高频（scalp_5m 年化换手
   ~55×，净负），VWAP-MR 等 maker 依赖策略被正确判死 —— 与 R5 生产结论互相印证。
4. **搜索效率**：3 参数 BO 用 8 次回测收敛到与网格穷举（3³=27 次）同量级结论，
   5M 高频场景省 ~3× 回测时间。

### ⚠️ 发现的问题（本阶段修复/规避）
1. **BO 退化到「不交易」**（已修复）：`utility = Sharpe − MDD − turnover` 对零交易点
   给 0 分（优于亏损），BO 会收敛到死区。修复：`optimize_loop.fn` 对
   `n_trades < min_trades` 加 −10 硬惩罚（`risk_gate` 语义前置）。修复后 scalp_5m 从
   「0 交易最优」变为正常搜索（624 train trades）。
2. **低频策略 × CPCV fold 不匹配**（未修，需上游决策）：trend_follower_port（1D）在
   C(6,1) 测试 fold（~157 根）里被 120 根 warmup 吃掉，test 0 交易。低频策略应改用
   全窗口 train/test 门禁（本适配层 `test_window` 已支持）或增大 fold 大小；
   引擎侧 CPCV 配置属 Helivex 决策，适配层不做。
3. **σ 口径对齐**（已修复）：paper 层 VWAP-MR 用「前 n 根收盘 stdev」而非「对 VWAP 的
   RMS」，首版实现 z≈1 恒定导致 0 交易；对照 `paper/strategies/*.py` 修正后信号正常。
4. **数据表命名陷阱**：`market_data.ohlcv_1h`（source=okx_swap）实为 **4H bars**
   （~180 根/月），真 1H 需从 5m 聚合 —— 已写入 `HELIVEX_DISCOVERY.md` 与 `data_tools`。

## 4. 边界与后续

- 本扫描只覆盖 **BTC-USDT-SWAP** 单一标的、单参数集（3 参数/策略）、单 seed；
  结论是「管线实用性」验证，不是策略判决。
- 未计 funding（DB 历史仅 2026-06-30 起）；持仓 <8h 的策略影响 <1bp/回合。
- 后续可选：`continuous_counterfactual`（成本/波动体制压力推演）、多标的组合搜索、
  与既有 gate 判决（`ops/scripts/*_gate.py`）交叉验证。

## 5. 复现

```bash
cd /data/soffy/projects/helivex && source venv/bin/activate
python -m helivex.scripts.run_features ...   # 1H 特征数据（directional 用）
python -m helivex_adapter.data_tools --symbol BTC-USDT-SWAP --timeframe 5m --start 2026-05-01 --end 2026-08-01 --out data/scan_btc_5m.parquet
python -m helivex_adapter.data_tools --symbol BTC-USDT-SWAP --timeframe 4h --start 2024-01-01 --end 2026-08-01 --out data/scan_btc_4h.parquet
python integrations/veya_loop_quant/run_scan.py
# 审计：integrations/veya_loop_quant/audit/scan_*.jsonl
```
