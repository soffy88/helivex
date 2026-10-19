# scalper_v2_port 策略优化 — 首次 Gate 验证（Veya Loop BO + 成本反事实）

> 目标：用 Veya Loop `bayesian_optimize` 优化 scalper_v2_port（5M ADX 滞回 + RSI/BB 双模式），
> 争取 test 区间过 risk_gate（跑正）。结论先行：**3 seed BO + 成本反事实均证明该策略
> 在 2026-04→08 BTC 5m 数据 + 现实成本下无净正解** —— 这是该策略的**首次 gate 验证
> （REJECT）**，与此前 paper 真实亏损（docs/STRATEGY_DIAGNOSIS_2026-07-22 §3.3，毛 −$12.4/20 天，
> 净值 −27.7）互相印证。

## 1. 前提与修正（诚实声明）

- **移植修正**：对照 `paper/strategies/scalper_v2_port.py:_evaluate`（2026-07 版）重写
  `helivex_adapter/strategies_port.py` 中的移植，修复 4 处失真：
  1. MR 入场改为**仅 RSI 极端触发**（原移植错误地要求 RSI+BB 双条件，过严）；
  2. 补齐 **SL/TP 括号**（SL = sl_atr_mult×ATR，TP = min_rr×SL，盈亏比 ≥1.2~2.5 结构性锁定）——
     原移植只有移动止损，赢小亏大；
  3. 补齐 **ATR 健康过滤**（atr/close ∈ [0.0005, 0.05]）、**EMA50/MACD 趋势一致过滤**（BO 模式）、
     **ADX<15 出场**（BO 模式）；
  4. ADX 滞回改为**真状态机**（mr↔bo + cooldown），原移植是逐 bar 即时判断。
  - 引擎差异（注明）：SL/TP 为 bar-close 检查而非触价单（更保守）。
- **数据**：BTC-USDT-SWAP 5m（2026-04-02 → 2026-08-01，34,463 根），真实 OKX 8h funding
  （`market_data.funding_rates`，358 根结算 bar 已 stamp）。
- **成本**：引擎 CostModel 默认 taker 5bps + slip 1bps（= 6bps 单边 / 12bps 往返），
  比 paper 假设（10bps 单边）乐观。
- **搜索**：6 参数（rsi_oversold / sl_atr_mult / min_rr / bb_k / trailing_atr_mult /
  cooldown_after_sl），train [04-02, 06-15) BO，test [06-15, 08-01) 门禁
  （min_sharpe 0.3 / max_drawdown 0.25 / min_trades 20 / max_turnover 50）。
  优化只发生在 train；未用 test 调边界。

## 2. BO 结果（3 seed）

| seed | n_evals | best（RSI / sl× / RR / bb_k / trail / cool） | train SR | train ret | ntr | test SR | test ret | ntr | 判定 |
|---|---|---|---|---|---|---|---|---|---|
| 0 | 20 | 15.7 / 1.24 / 2.23 / 2.47 / 1.05 / 9.75 | −0.037 | −21.2% | 911 | −0.027 | −13.8% | 557 | REJECT |
| 1 | 12 | 21.5 / 1.44 / 2.46 / 2.49 / 1.06 / 9.92 | −0.037 | −21.2% | 901 | −0.030 | −15.3% | 543 | REJECT |
| 2 | 12 | 21.4 / 1.36 / 1.23 / 2.43 / 1.03 / 11.09 | −0.042 | −23.8% | 1013 | −0.043 | −17.4% | 599 | REJECT |

- BO 稳定收敛到同类区域：**更极端 RSI（~20/80）、更长止损冷却（~10 bar）、更宽 BB（~2.45σ）**。
- 与 paper 默认（1716 笔 train）相比，最优把交易频率**减半**（~900 笔）、成本占比
  0.41→0.24，效用提升；但 net 依旧深度为负 → **不是搜索没找到，是解不存在**。

## 3. 成本反事实（同一组参数，干预成本假设）

| taker 比例 | 单边成本 | train SR | train ret | test SR | test ret |
|---|---|---|---|---|---|
| 100% taker | 6 bps (5+1) | −0.052 | −39.2% | −0.051 | −26.2% |
| 100% taker | 2 bps (2+0) | −0.015 | −14.2% | −0.013 | −9.0% |
| 50% taker | 6 bps | −0.036 | −30.8% | −0.036 | −20.2% |
| 0% taker (全 maker) | 6 bps | −0.023 | −21.3% | −0.022 | −13.7% |
| 0% taker | 2 bps (4 bps RT) | −0.015 | −14.2% | −0.013 | −9.0% |

- **即使 2bps 单边（4bps 往返，低于 R5.4「最乐观 4bps 双腿 maker」）仍深度为负**；
- 线性外推零成本：gross ret ≈ **+2%**（gross Sharpe ≈ +0.02）——**信号本身在这段数据上
  无 alpha**，不是成本把正收益吃成负，而是毛收益本来就 ≈ 0。

## 4. 结论

1. **无法跑正**：在 2026-04→08 BTC 5m 数据、默认成本模型、YAML 文档化信号定义下，
   net 正 Sharpe 的解不存在于该参数空间。3 seed BO + 成本反事实交叉确认。
2. **生产建议**：维持 `trade_enabled: false`（observe）。本次 gate 验证 = REJECT，
   与 paper 真实亏损（全组合最大真实亏损源）一致；「UNVALIDATED」状态应更新为
   「VALIDATED-REJECT」。
3. **要跑正需满足（量化门槛）**：gross 收益需从 ~+2% 提升至 ≥ +20%（12bps RT 成本下
   ≈ 净正门槛），即信号级改进（入场/出场逻辑重设计、跨标的/跨周期），超出参数优化范围。
   或获得 <2bps 单边的执行（OKX maker rebate 场景），仍只够把 −0.015 拉回 ≈ 0。
4. **Veya Loop 实用性结论**：BO 正确地把搜索推向成本最优区（交易减半），门禁正确拦截
   （无假阳性）；「跑不正」是策略/市场事实，不是工具失效。

## 5. 1H 周期变体（按指令追加，2026-08）

把 scalper_v2_port 信号移植到 **1H 周期**（策略矩阵既有档位；数据 = 5m 聚合 1H，
2873 根，带真实 funding）。参数按周期缩放：max_holding 48×5m=4h → 1H 下 2~8 bar（入搜），
cooldown_bars 4→1。成本占比从 5m 版的 41% 降到 ~3%，交易频率降 12 倍。

| seed | n_evals | train SR | train ret | train cost | test SR | test ret | test ntr | 判定 |
|---|---|---|---|---|---|---|---|---|
| 0 | 30 | −0.015 | −2.6% | 3.1% | −0.032 | −4.1% | 60 | REJECT |
| 1 | 30 | +0.004 | −3.6% | 2.9% | −0.035 | −5.1% | 58 | REJECT |
| 2 | 30 | +0.022 | −0.8% | 3.1% | −0.023 | −3.6% | 62 | REJECT |

**关键证据**：1H 周期把成本占比压到 3%（成本碾压不再是主因），train Sharpe 已回到 ≈0
（seed 2 甚至 +0.022），但 **test 全部为负** —— 实锤：scalper_v2_port 信号本身在
这段 BTC 数据上 **gross 无 alpha**（≈0 甚至略负），与 §3 成本反事实的零成本外推
（gross +2%）互相印证。1H 不能“跑正”，因为问题不在周期/成本，在信号。

## 6. 复现与审计

```bash
cd /data/soffy/projects/helivex && source venv/bin/activate
python -m helivex_adapter.data_tools --symbol BTC-USDT-SWAP --timeframe 5m \
    --start 2026-04-02 --end 2026-08-01 --out data/scan_btc_5m.parquet
# BO（seed 0，20 次回测 ≈ 8.5 min）
# 审计：integrations/veya_loop_quant/audit/opt_scalper_v2.jsonl（含 train/test 指标）
# 成本反事实：integrations/veya_loop_quant/audit/scalper_cost_counterfactual.jsonl
python integrations/veya_loop_quant/run_cost_counterfactual.py
# 1H 变体审计：integrations/veya_loop_quant/audit/opt_scalper_v2_1h.jsonl（3 seed × 30 次回测）
```
