# HELIVEX-IMPL_SPEC-TPB-001 v1.0 — trend_pullback_v1

**策略**:regime门控趋势回调(maker执行 / 右尾出场 / 正交veto)
**状态**:Phase R 完成 → **配置已冻结(2026-07-19)** → 待单次验证 → 判决
**Trial**:全局 #29(见冻结项 F1),DSR 门槛 2.0594
**族关系**:ATR追踪翻转族的回调重构版。本 spec REJECT → 该族永久关闭。

---

## Part I — Phase R 勘察结果(实证,非假设)

全部结论来自对生产库/仓库的直接查询,查询语句见本次会话记录。

### R1 数据覆盖

**关键发现:`source` 命名与真实周期不符**,这是本 spec 最容易踩的坑:

| source | **真实 bar 间隔** | 三资产交集覆盖 | 用途 |
|---|---|---|---|
| `okx_swap` | **14400s = 4h** | 2021-01-22 → 2026-07-19(2004 天) | regime 框架 |
| `okx_swap_1h` | 3600s = 1h | **2024-07-20 01:00 → 2026-07-10 00:00(719 天)** | 交易/执行框架 |

(表名 `market_data.ohlcv_1h` 同样是历史遗留误导 —— 它是所有周期的公共表,靠 `source` 区分。)

**断档(G2.3)**:1h 零处 >6h 断档;4h 零处 >24h 断档。三资产均如此。**受影响交易预期计数 = 0**。

**危机窗(G2.1 / T5)**:

| 窗口 | 1h bars | 4h bars | 纳入? |
|---|---|---|---|
| 2024-08-05 ±3d | 168(完整) | 42 | ✅ 纳入 |
| 2025-10-10 ±3d | 168(完整) | 42 | ✅ 纳入 |
| FTX 2022-11 | **0** | 60 | ❌ 不纳入(1h 无覆盖,按 G2.1「若覆盖则纳入」的字面) |

**warmup(G2.4)**:4h Ichimoku(52+26位移=78 根 4h ≈ 13 天)与 4h ADX(14) 的预热完全落在 1h 窗口起点之前(4h 数据早 3.5 年),不构成约束。1h 侧约束:Kijun(26) 需 26 根、ATR(14) 需 15 根 → **首个合法信号最早 2024-07-21 03:00 UTC**。

### R2 funding 可达性

helios 库从 helivex 侧**可连**(同 `platform-postgres:5434` 实例,同 `helios` 角色)。`public.raw_features` 有 616,911 行,`feature_name='funding_rate'` 194,842 行。

| source | 覆盖 | 采样 |
|---|---|---|
| **dydx** | **2025-05-07 → 2026-07-19(438 天 / 14.4 月)** | 1h(`meta={"interval":"1h"}`) |
| hyperliquid_ctx | 2026-06-24 → 2026-07-19(25 天) | ~31s |
| coinalyze | 2026-07-02 → 2026-07-19(17 天) | ~35s |

**dydx 数值口径**:小数形式的 1h 费率。BTC:p10=-0.0000234、p50=+0.0000021、p90=+0.0000241(即 p50 ≈ 0.021 bps/h)。

**覆盖缺口(决定 F4)**:主窗口起点 2024-07-20,dydx 起点 2025-05-07 → 主窗口前 291 天(40%)无 funding;再扣 180d trailing 分位预热 → veto 真正可用要到 2025-11-03,**仅覆盖主窗口最后 250 天(34%)**。

### R3 v51 验证层接口

- **`simulate()` 确认为 close-to-close**:`simulator.py:65` → `bar_pnl = position_fraction * bar.close_return`。表达不了限价成交/止损穿越/同bar SL优先 → **必须自写盘内 runner**(与 N1 判断一致)。
- **`dsr(sr_hat, n, skew, kurt, var_sr_trials, n_trials)` 是纯函数** → 可直接喂外部交易收益序列 ✅
- **`pbo(is_sharpes[n_splits, n_configs], oos_sharpes)` 是纯函数**,但 `n_configs ≥ 2` 才有意义(单配置时 argmax 恒 0、median 恒等于自身 → 退化)→ 决定 F5。
- `CPCVConfig` 默认:`n_groups=10, n_test_groups=2, embargo_bars=0, purge_overlap=True` → C(10,2)=45 splits。**默认 embargo=0** → 决定 F6。

### R4 SuperTrend 现状 —— **spec 前提不成立**

**仓库中不存在 SuperTrend 策略文件**(`find -iname "*supertrend*"` 空)。SuperTrend 仅以两种形态存在:
1. `oprim.technical.trend.supertrend()` —— 一个指标原语;
2. `strategies/trend_dual.yaml` / `spot_trend_1d.yaml` 的 `indicators:` 配置段 —— **仅供 gate 回测读取,实盘不消费**。

实盘 `paper/strategies/donchian_4h.py` 跑的是 **Donchian 通道**,与 SuperTrend 无关(YAML `description` 写 "SuperTrend+EMA+ADX+MACD" 是历史误导,已在 `STRATEGY_OPTIMIZATION_2026-07-19.md` 记录为文档卫生问题)。

后果:R4「标记 deprecated」无对象;§11 可选诊断的前提(旧 SuperTrend 策略 + RSI>70 否决)同样不成立 → **§11 取消**(见 F7)。

### R4b 落位约定

`ops/scripts/` 是全部 12 个既有 gate 脚本的现行位置,§10 交付路径与仓库现实一致,**无冲突**。

---

## Part II — 冻结快照(2026-07-19,冻结后禁止修改)

### 数据驱动分支的最终取值

| # | 项 | 冻结值 | 依据 |
|---|---|---|---|
| **F1** | **Trial 编号 / N** | **#29,N=29,DSR 门槛 2.0594** | `.gate_trials.json` `total_trials=28`,history 完整 #1–#28,末条为 #28(Compound-B2)。全仓库无 #29 痕迹。按仓库既定惯例 `n_trials = total_trials + 1`(`gbm_compound_b2_gate.py:478`)。**spec 预期的 #30 不成立** —— 其假设的「#29 = ATR追踪翻转族」从未登记。 |
| **F2** | **主窗口** | **2024-07-20 01:00 → 2026-07-10 00:00 UTC(719 天)** | 1h 交易数据的三资产交集。spec「期望≥2021起」不可达 —— 4h 有 5.5 年但交易框架是 1h。 |
| **F3** | **危机窗集合** | 2024-08-05±3d、2025-10-10±3d。**FTX-2022 排除** | 1h 侧 FTX 窗 0 bars。 |
| **F4** | **funding veto (V1)** | **OFF** | dydx 仅覆盖主窗口 34%(扣 180d 预热后)。若 ON 则窗口前 66% 与后 34% 是两个不同策略,污染统计检验。处理方式与 spec 对 V3 级联veto 的既定处理一致(「无历史→回测中OFF,标记 live-only 增强」)。**回测跑的是无 funding veto 的保守版**。 |
| **F5** | **PBO 的 config 维度** | **用 T3 的 7 个配置(中心+6扰动)** | PBO 数学上需 `n_configs ≥ 2`。PBO 内部的 argmax 是统计量定义的一部分,**不等于采用最优配置** —— §7 T3「禁止挑最好的一组替换中心」的纪律不受影响。 |
| **F6** | **CPCV embargo** | **`embargo_bars = 50`**(偏离 spec N3 字面) | spec N3 说「按 v51 默认」,但 v51 默认是 **0**(无 embargo)。选 50 的三条依据:(a) helivex 全部既有 gate 与所有策略 YAML `gate.embargo_bars` 均为 50;(b) 50 ≈ X4 时间止损的 48 根最大持仓,能真正隔离 train/test 的交易重叠,embargo=0 会让边界交易泄漏;(c) 更严格 = 更重举证负担,符合本 spec「在最重举证负担下过 gate」的精神。**这是本冻结快照唯一一处对 spec 字面的主动偏离,理由前置声明,非看结果后调整。** |
| **F7** | **§11 可选诊断** | **取消** | 前提对象(SuperTrend 策略)不存在。 |
| **F8** | **V2 OI veto** | 排除(spec 预注册) | 无变更。 |
| **F9** | **V3 级联veto** | 回测 OFF,标记 live-only | 无变更。 |
| **F10** | **funding 成本 (C5)** | 2025-05-07 前:**1bp/8h 保守代理,无论方向均为支付**;之后:dydx 1h 真实费率(多头付正、空头收正) | C5 明文「不许设0」。代理值 0.125 bps/h vs dydx 实测 p50 0.021 bps/h → 代理贵 6 倍,符合保守意图。 |

### 策略参数(§4 预注册,原样冻结)

```
regime框架=4h(source=okx_swap)  交易框架=1h(source=okx_swap_1h)  多空对称
RG1 close_4h > max(senkou_a, senkou_b)      Ichimoku 9/26/52
RG2 ADX(14)_4h > 25
E1  regime开 且 close_1h > Kijun(26)_1h → 在 Kijun 挂 post_only 限价
E2  每根1h收盘 cancel-replace 更新限价,不追价
E3  地板:限价 ≥ 4h云顶,否则不挂
E4  regime翻转 或 close_1h 跌破云顶 → 撤单
E5  成交判定:下根1h bar 的 low < 限价(严格小于,touch不算),成交价=限价
X1  初始止损 = entry − 2.0×ATR(14)_1h(成交时刻冻结)
X2  +2R(R=2×ATR_entry)reduce-only 平50%,high > tp 严格穿越;成交后止损→breakeven
X3  吊灯 trail = 入场以来最高close − 3.0×ATR(14)_1h(当前bar);有效止损单调只升不降
X4  48根1h bar 内最大有利偏移 < +1R → 收盘价平仓
X5  4h regime(滞后对齐)转 false → 下一1h收盘平仓
X6  同bar歧义:SL 优先于 TP;入场bar内 low ≤ 止损 → 当bar止损
P1  单笔风险 0.5%权益:qty = 0.005×equity / (2×ATR_entry)
P2  三资产合计名义 ≤ 1.0×权益,超限跳过并记 capacity 事件
P3  连亏3笔→停新单24h;单日已实现亏损>2%权益→停至次UTC日
P4  杠杆 1x
P5  三资产共享权益的事件驱动组合(gate 判决以组合为准)
C1/C2 maker 2bps    C3 taker 5bps + 1bp滑点    C5 见 F10
```

### 判决标准(§8,原样冻结)

PASS 需**全部**满足:组合 net DSR > 2.0594 **AND** CPCV-PBO < 0.5 **AND** 组合 net Sharpe > 0 **AND** ≥2/3 资产 net > 0 **AND** 扰动 ≥6/7 gross > 0。

**KILL-1**:组合 gross Sharpe ≤ 0 → 立即 REJECT,不看后续指标。

其余一切情形 = REJECT。REJECT 后 ATR追踪翻转族(含本回调重构)永久关闭,报告中不得夹带救活方案。

---

## Part III — 验证结果与判决

冻结配置单次运行,2026-07-19。数据:三资产各 17,280 根 1h bar,2024-07-20 → 2026-07-10(720 天)。
结果 JSON:`ops/reports/tpb_v1_result.json`。已登记 `.gate_trials.json` **trial #29**。

### 判决:**REJECT**(5 项判据 4 项失败)

| §8 判据 | 门槛 | 实测 | |
|---|---|---|---|
| 组合 net DSR > 真实N门槛 | > 2.0594 | **−0.791**(adj −2.850) | ✗ |
| CPCV-PBO < 0.5 | < 0.5 | **0.871** | ✗ |
| 组合 net Sharpe > 0 | > 0 | +0.009 | ✓ |
| ≥2/3 资产 net > 0 | ≥ 2 | **1/3** | ✗ |
| 扰动 ≥6/7 gross > 0 | ≥ 6 | **5/7** | ✗ |

KILL-1 未触发(组合 gross Sharpe +0.618 > 0),故按 §7 走完了全部 T3–T6。

### T1 中心配置 + §5 C6 成本分解

550 笔交易,gross Sharpe +0.618,net Sharpe +0.009。

```
gross            +1214.05
− maker            348.31
− taker+滑点       678.27
− funding          170.39
= net              +17.08
```

**成本吃掉 gross 的 98.6%**。其中 taker(678)接近 maker(348)的两倍 —— 入场侧的 maker 设计
生效了,但出场侧 88%(485/550)是止损单走 taker,成本节省只实现了一半。

### T3 扰动网格(仅符号检验)

| 配置 | n | gross | net |
|---|---|---|---|
| center | 550 | **+0.618** | +0.009 |
| adx20 | 711 | **−0.526** | −1.230 |
| adx30 | 436 | **−0.269** | −0.853 |
| sl1.6 | 562 | +0.040 | −0.592 |
| sl2.4 | 554 | +0.616 | +0.002 |
| trail2.4 | 631 | +0.851 | +0.123 |
| trail3.6 | 502 | +0.823 | +0.340 |

gross>0 仅 5/7。ADX 阈值双向扰动(20 与 30)**都翻负**,中心值 25 处于参数悬崖上而非稳健区间。

### T4 验证层

- mean_oos Sharpe −0.044;DSR(mean−std)−0.791,门槛 2.0594,adj **−2.850**
- PBO_cscv **0.871**;跨配置 PBO(F5)0.500;IS>OOS 频率 0.533
- Bailey-LdP 真 DSR 概率(non-gating,n=550 笔)**0.0159**
- 前后半段 net Sharpe:**−0.954 / +0.776,符号不一致**
- 每资产 net:BTC −643.37、ETH +806.40、SOL −145.95 → 组合 +17.08 完全来自 ETH 单一资产

### T5 危机窗

| 窗口 | 入场 | net | regime 门 | 熔断 |
|---|---|---|---|---|
| 2024-08-05 ±3d | 8 | **+381.96** | 开闸(有入场) | 未触发 |
| 2025-10-10 ±3d | 7 | **−209.28** | 开闸(有入场) | 未触发 |

两个窗口 regime 门都开过闸并有入场,未出现"在瀑布中接刀"的单窗灾难性亏损。
FTX-2022 按 F3 排除(1h 无覆盖)。

### T6 频率

- 550 笔 / 720 天 / 3 资产 = **0.255 笔·天⁻¹·资产⁻¹**,平均持仓 20.1 根 1h bar
- **挂单 5,249 → 成交 550,maker 成交率 10.5%** ← 这是将来 paper 实测 fill rate 的对照基准
- 被 E5 严格判定(low 必须 < 限价)拒掉的 touch:**1 笔**,该保守规则几乎不影响本次结果
- capacity 事件(P2 净敞口上限)0 次
- 出场原因:stop 344、stop_be 129、regime 62、stop_entrybar 12、time 3
  (stop_be = 已吃到 +2R 分批止盈后回落到 breakeven → 有 129 笔真正触及过右尾目标)

### 族封棺

按 §8,REJECT → ATR追踪翻转族(含本回调重构)永久关闭。

**登记表事实的诚实修正**:spec 设想的"两钉封棺(#29+#30)"在登记表上只有一钉。Phase R 查明
spec 引用的前一次族内探索(36 配置 gross 全负)**从未登记进 `.gate_trials.json`**,全仓库无痕迹。
因此本次 #29 是该族**唯一一次进入登记表的 gated trial**,判决 REJECT。

### 过程偏差记录(诚实披露)

1. **登记早于计划**:为在登记前先核验管线,我给 gate 加了 `--register` 开关(默认不登记),
   但补丁只应用了一半 —— 函数签名与 CLI 标志生效了,`_emit` 的函数体没被替换,因此首次运行
   即无条件登记。事后核验:成本恒等式、出场原因合计、成交数、每资产 net 合计四项内部一致性
   **全部通过**,登记的是算术自洽的正确结果,登记表未被污染。该 bug 已修复(仅影响将来复用)。
2. **F6b 新增**:`purge_bars=48` 在冻结快照中未单列。它由已冻结的 X4 最大持仓(48 根)直接导出,
   与 F6(embargo=50)同一依据,非看结果后选取。
3. **PBO 双口径**:F5 冻结了跨配置 PBO,但 R3 之后发现仓库标准 `pbo_cscv` 用块自助生成配置维度、
   单配置即可计算且与 #1–#28 可比。两者均已报告(0.871 / 0.500),判决采用仓库标准口径。
