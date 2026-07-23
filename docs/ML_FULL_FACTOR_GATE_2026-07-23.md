# ml_lgb 全量因子过门实验(2026-07-23)

## 背景

helixa→helivex 功能对照审计发现:P4b 移植 qlib-v2 时因子面从 Alpha158 全家桶
(价格+成交量+量价+区间+微观结构,~50 维)砍到 close-only 16 维,且无实验依据、
无文档记录。遗留问号:**"模型不 predictive"到底是因子不够,还是本来没有 alpha?**
现有证据分不清。本实验补齐全量因子,过真门禁裁决。

## 改动

- `oskill/signal/ml_feature_matrix.py`:新增可选 `opens/highs/lows/volumes` 入参,
  齐全时追加 helixa 因子(vol_ratio/vol_std、vwap_dev、pv_corr、rsi_6/24、boll、
  K线形态、high/low_break+position、atr、efficiency),15 维 → 51 维。
  close-only 路径不变(向后兼容)。部分给参 → ValueError(拒绝静默降级)。
- `omodul/ml_signal_workflow.py`:透传 OHLCV;findings 增加
  `n_features/full_ohlcv/oos_sharpe_net/fee_drag_pct`。
- `ops/scripts/signal_engines_adapter.py`:`_ohlcv` 取全五列,ml_lgb 喂全量。

## 结果(okx perp 5m,~5500 bars ≈ 19 天,WFV train500/test100/step100)

### 第一轮:无摩擦门(改动前的门口径)

| 标的 | 因子 | 准确率 | OOS Sharpe(毛) | DSR | promoted |
|---|---|---|---|---|---|
| BTC | 15 维 | 0.4929 | −3.82 | 0.00 | ✗ |
| BTC | 51 维 | 0.5115 | +0.11 | 0.00 | ✗ |
| ETH | 15 维 | 0.5095 | −6.14 | 0.00 | ✗ |
| ETH | 51 维 | **0.5333** | **+3.84** | **1.00** | **✓** |
| SOL | 15 维 | 0.5000 | +1.99 | 1.00* | ✗(准确率≤0.5) |
| SOL | 51 维 | **0.5202** | **+6.05** | **1.00** | **✓** |

**全量因子确实带来真实的毛预测力**——问号的答案是"因子不够"占一部分:
成交量/区间/微观结构因子把 ETH/SOL 从负 Sharpe 抬到显著正,毛口径过门。

### 成本墙检验(taker 5bps × 仓位变化,与 gateway FIFO 口径一致)

| 标的 | 非中性预测占比 | 毛收益 | 手续费 | 净收益 | 净 Sharpe |
|---|---|---|---|---|---|
| BTC | 5.1% | +0.06% | 6.5% | −6.44% | −11.8 |
| ETH | 9.5% | +2.35% | 10.8% | −8.45% | −13.4 |
| SOL | 10.1% | +4.53% | 13.0% | −8.47% | −11.0 |

**手续费是毛 alpha 的 ~3–100 倍。** 与 R5(taker scalp 307%/yr)、复合 B2
(成本墙)完全同构:5m 换手频率下,任何 <2bps/bar 量级的毛边际都活不过 taker 费。

### 门禁修正(本实验的真正产出)

无摩擦门会晋级一个费后亏 8% 的模型 → **门是成本盲的,违背"门是承重的"原则**。
已修:`ml_signal_workflow` 的 DSR 门改为**费后口径**(`taker_fee_bps=5.0`,
按 OOS 预测序列隐含的仓位变化计费;毛 Sharpe 保留在 findings 仅作观察)。
修后重跑:6/6 组合全部正确拒绝(净 deflated Sharpe −12.6 ~ −22.6)。

## 结论

1. **"全量因子也救不了"现在有实验背书了**——但死因和 helixa 时代的判词不同:
   不是"模型不 predictive"(毛口径它 predictive),是**5m 频率的成本墙**。
   结案口径应为:ml_lgb 在 5m 上毛 alpha 真实但费后必死,晋级正确拒绝。
2. 若要救,方向不是再加因子,而是**降频**(1h/4h bar 上重跑,换手降 12–48 倍)
   或 maker 化(参见 scalp_5m maker 实验)。1h 数据现成(719d),成本约为
   一次 gate 重跑;做不做待人工裁决。
3. 生产已切换:adapter 喂 51 维,门费后口径,observe-only 不变。
   端到端验证:`paper.engine_signals` 最新 ml_lgb 行含
   `n_features=51, oos_sharpe_net, fee_drag_pct, promoted=false`。

## 复现

- 探针:scratchpad `ml_full_features_gate.py` / `ml_cost_probe.py`
  (成本探针毛 Sharpe 与 workflow 输出逐位一致,口径已互验)。
- 测试:oskill tests/signal 14 passed;omodul tests/test_signal_workflows.py 6 passed。
- 多重检验:dsr_n_trials=10 未随本轮试验数上调;因结论是"拒绝",
  低估试验数只会让拒绝更保守,不影响判决方向。
