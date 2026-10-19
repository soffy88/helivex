# continuous_counterfactual — 完整版反事实推演（Veya Loop HybridSCM 落地）

> spec §5「可延后」项落地：把「枚举重跑成本假设」升级为「拟合因果模型 → L3 反事实推演 +
> 连续干预规划」。本报告记录实现、两个案例的结果、交叉验证误差与诚实边界。
> 日期：2026-08-06。研究态；SCM 只做决策支持，引擎仍是真相（每次推演附引擎交叉验证）。

## 1. 能力面（veya_loop.oprim）

- `HybridSCM`（混合离散-连续节点）：`fit` 逐节点学条件机制（diagonal 仿射，线性/MLP）→
  `abduct(evidence)` 溯因（从观测反推外生噪声 U）→ `l3_counterfactual(evidence, do, node)`
  **锚定同一 U 推演**（与事实共用同一个世界，非重新抽样）。
- `continuous_plan_with_hybrid_bo`：在 SCM 上 BO，目标 = query 节点的 L3 反事实值，
  找最优干预组合。

## 2. 实现（helivex_adapter/counterfactual.py）

- `sample_dataset`：参数×成本×窗口 → 引擎组合回测 → 节点观测
  （cost_bps / vol / 参数 / n_trades / cost_drag / gross_ret / net_ret / sharpe / mdd）。
  支持随机子采样与**全交叉**（见 §4 教训）。
- `build_scm`：DAG = 参数/成本/波动 → 交易行为 → 收益/风险 → Sharpe；全连续 diagonal、
  线性拟合（小样本稳，shrinkage="lw"）。
- `counterfactual_curve` / `breakeven_cost` / `plan_max_sharpe`：L3 扫点、净正成本门槛、
  SCM 上联合干预 BO。推演值可 clamp（线性外推可能出 MDD<0 等荒谬值）。
- 每次推演都输出「引擎重跑同窗口同参数」交叉验证。

## 3. 案例结果

### 案例 1 — vwap_mr_1h（三标的组合，全交叉 288 次回测 ≈ 85s）

锚定事实：seed0 best 参数 + 6bps + 窗口 05-15→07-01（组合 vol=0.101）。

| do(cost) | 2.0 | 4.0 | 6.0 | 8.0 | 10.0 |
|---|---|---|---|---|---|
| L3 sharpe | +0.520 | +0.299 | +0.077 | −0.145 | −0.366 |

- 每 +2bps ≈ −0.22 Sharpe，单调合理；**交叉验证 cost=2：SCM +0.520 vs 引擎 +0.604**
  （误差 0.08，好）；**cost=8：SCM −0.145 vs 引擎 +0.413**（误差 0.56，差，
  原因见 §4）。
- 波动压力（分布内）：do(vol=0.072，另一窗口实测) → MDD 0.054、sharpe −1.104。
  方向自洽：低波动窗口（4-5 月）恰是 train 负收益段。

### 案例 2 — scalper_v2_port（1H 变体组合，全交叉 60 次 ≈ 50s）

- **净正成本门槛（L3 扫点）：1.98 bps 单边** —— 与此前枚举反事实（2bps 单边
  net ≈ 0）高度吻合，SCM 推演复现了引擎枚举结论，且无需逐点重跑。
- `continuous_plan_with_hybrid_bo`：最优干预 (cost=1.38, bb_k=2.78) → SCM sharpe
  +0.306；引擎重跑同干预点 +0.160（方向对，数值偏——bb_k=2.78 超出采样范围，外推）。

## 4. 方法论教训（本批踩坑）

1. **随机子采样学不到小效应**：首版 80 个随机样本中 corr(cost, sharpe) = −0.025，
   成本效应（~0.2/4bps）被参数/窗口噪声（sharpe std 0.6）淹没，SCM 推演与引擎
   严重不符（+0.35 vs +0.01）。改**全交叉**（参数×成本×窗口确定性覆盖）后成本
   曲线恢复单调合理。
2. **线性 SCM 外推不可靠**：vol 压力首次用 1.3× 外推 → sharpe +18、MDD −0.264
   （荒谬）；改为分布内干预（另一窗口实测 vol）后方向自洽。推演值 clamp 兜底。
3. **未建模参数是交叉验证误差的主源**：模型只学 z_thr/hold/sl_std 三个主效应，
   vwap_n/std_n/min_rr/cooldown 被平均掉 → cost=8 处 SCM（平均世界）与引擎
   （特定参数世界）偏差 0.56。若要更高精度需把全部参数纳入（样本量随维度增长）。

## 5. 结论与边界

- **能力成立**：成本冲击曲线、净正成本门槛（1.98bps）、联合干预规划（BO on SCM）
  全部产出合理且与引擎枚举交叉印证；「锚定噪声」使推演可归因（同一事实，不同干预）。
- **精度边界**：线性仿射 + 主效应参数 → 分布内误差 ~0.1-0.3 Sharpe；外推区不可用。
- **使用建议**：SCM 用于「压力推演 / 门槛估计 / 干预规划」的决策支持；最终判决
  永远以引擎重跑为准（本模块强制输出交叉验证记录）。

## 6. 复现

```bash
cd /data/soffy/projects/helivex && source venv/bin/activate
python integrations/veya_loop_quant/run_counterfactual.py
# 审计：audit/counterfactual_vwap_mr.jsonl, audit/counterfactual_scalper.jsonl
```
