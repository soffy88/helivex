# HELIVEX-IMPL_SPEC-LER-001 — 爆仓衰竭回归策略（LER）实现规格

版本: v1.1（Phase R 勘察后修订,见 §12）| 状态: Phase R 完成,已获 Wiki 裁决 → Phase 0 待数据积累
执行模式: FULL AUTO（门失败即停机上报，不自行裁决）
目标仓库: helivex（实际路径 /data/soffy/projects/helivex,已由 Phase R 勘察确认；上游采集在
sibling repo `iris`,见 §3/§12）
交付定义: merged to main + CI 全绿 + 判决报告落库 + 全部 commit hash 可复核

---

## 0. FULL AUTO 执行规则

- CC 全自动执行。仅三种情形停机上报：(1) 任一 Gate 失败；(2) 规格与仓库现实冲突且无预授权裁决路径；(3) 预注册配置预算耗尽。
- 禁止：静默改参、静默降低验收标准、编造数据覆盖、用"接近通过 / 基本达标"类措辞软化判决。
- 每个 Phase 结束提交阶段报告 + commit hash 列表。最终报告的一切结论必须可由 git log 与落库工件独立复核。

## 1. 背景与假设（CC 不需重新论证）

H0：大规模单边爆仓是价格不敏感的强制平仓流，造成短时超调；当瀑布衰竭（爆仓速率崩塌 + OI 停止收缩）时，超调部分均值回归。多空对称。

本 spec 的职责是把 H0 送进验证管线并接受判决，不是让它通过。REJECT 是合法产出。

## 2. 范围

**In-scope**：数据审计门、oskill 计算层、LER 信号 omodul、事件驱动交易模拟接入 helios_backtest_v51、CPCV/DSR 判决、报告。

**Out-of-scope（明确禁止）**：
- 实盘/模拟盘下单、交易所密钥、订单路由——本 spec 纯研究与回测
- HMM regime omodul 集成：Phase R 未能定位到 spec v1.0 描述的"三个复合缺陷"的任何文档依据(不存在)。禁用 HMM 依赖的真实依据改为 helivex 既有研究结论——commit `646dc71`("HMM regime direction exhausted & closed — 11/11 FAIL")、`docs/R11_HMM_REGIME_COMBO_GATE.md`、`docs/R12_XASSET_REGIME_GATE.md`:加密主流币种 HMM regime 无 OOS 持续性,11/11 已跑过的相关尝试全部 FAIL。v1 使用 §5.4 确定性过滤器,不引入 HMM 依赖。
- 多币种扩展

## 3. 依赖与前置（Phase R 后修订,原 v1.0 假设见 §12.1 冲突清单）

- **数据源**：~~coinalyze 聚合爆仓~~——Phase R 确认 coinalyze 在本机基础设施里不存在爆仓采集(仅在无关的 `helios` 项目里采 OI/funding),该主源假设不成立。**改为 OKX(经 sibling repo `iris` 的 `stream-okx` collector,`md.liquidations`,BTC/ETH/SOL-USDT-SWAP)**,已在跑,2026-07-06 起有数据。Binance forceOrder 流经 Phase R 确认在本机不可用(geo-blocked,`helios` 项目已放弃),**不再作为交叉校验源**,G0.2 交叉校验判据随之取消(见 §4)。Funding/OI/OHLCV 同样改为 OKX 口径,经 `iris` → `md.*` → helivex `*_from_md.py` adapter 链路(与 helivex 近期完成的采集层合并一致,见 commit `12a790c`/`f708e9f`/`489d877`)。1m OHLCV 由 iris commit `93ba851`/`18bb3e9`(2026-07-08)补齐(`ohlcv_okx_series` 加 m1),已部署,`md.ohlcv` 已有数据。
- **引擎**：helios_backtest_v51（CPCV + purge/embargo + PSR/DSR）——Phase R 确认实际存在且正确实现于 `platform/3O/helios_backtest_v51/`,采用之(而非 helivex 现有的、自认非纯化的历史遗留 `tools/strategy_gate.py`)。
- **规范**：~~HELIOS_3O_SPEC v0.7~~ → **实际当前版本 v3.0**(`platform/3O/HELIOS_3O_SPEC_v3_0.md`,2026-06-12)。命名约定：Phase R 确认无 `oskill_xxx`/`omodul_xxx` 前缀,实际为裸 snake_case 文件置于 `oskill/oskill/`、`omodul/omodul/` 目录下,`from oskill import xxx` 方式导入。四支柱为 omodul 层最低要求 ≥1 个(非强制全 4 个),本 spec 主动选择全部 4 个,详见 §8。

**Phase R（勘察，编码前强制）—— 已完成,结论见 §12。** 数据源假设(coinalyze/Binance forceOrder)与仓库现实冲突,已由 Wiki 于 2026-07-08 裁决:不采购付费数据商、不构造代理信号,改为**即时起对 OKX 正向采集**,放弃逐笔历史爆仓数据的深史回填要求(24 个月主窗口 + 强制 FTX-2022/COVID-2020 覆盖,数据源头上不可能满足,见 §4 修订)。

## 4. Phase 0 — 数据审计门（BLOCKING，Phase R 后修订）

目的：证明 LER 建在干净地基上。原判据依赖的"已修复 coinalyze 午夜桶 bug"、forceOrder 交叉校验、24 个月/双危机窗覆盖,均由 Phase R 确认不成立或不可得,已由 Wiki 于 2026-07-08 裁决改为**滚动正向采集**口径。判据修订如下:

| Gate | 判据（修订） |
|---|---|
| G0.1 爆仓时间戳保真 | 分钟桶分布中 00:00 UTC 桶事件数 ≤ 全体分钟桶均值 × 3（沿用同一统计判据,校验 OKX `stream-okx` 采集本身无时间戳截断类 bug,不再假定"修复前" bug 历史） |
| G0.2 爆仓覆盖 | 窗口内 ≥ 95% 交易日有非零记录。**forceOrder 交叉校验判据取消**（Binance forceOrder 在本机不可用,无可交叉校验的第二源） |
| G0.3 funding 完整性 | 事件数 = 3/日 × 日数，缺口 = 0；随机抽 30 个点与 OKX 交易所公开历史全对 |
| G0.4 OI 完整性 | 分钟缺口率 < 0.5%；触发窗口内无 > 15m 连续缺口 |
| G0.5 OHLCV 完整性 | 1m K 线缺口率 < 0.1%（数据起点 2026-07-08,iris commit `93ba851` 部署时刻） |
| G0.6 窗口覆盖矩阵 | 输出各数据源自 2026-07-06(爆仓)/2026-07-08(1m OHLCV) 起的滚动覆盖天数,**不再强制 FTX-2022/COVID-2020**(见下) |

**主窗口定义（修订）**：数据可支持的最长连续区间，起点为各数据源实际采集起点（爆仓 2026-07-06、funding/OI 2026-06-30 起、1m OHLCV 2026-07-08 起，逐日滚动增长）。**24 个月最低要求与 FTX-2022/COVID-2020 强制覆盖已由 Wiki 裁决撤销**——数据源头（OKX 无历史回填能力、Binance 官方历史归档不含 USDT 本位逐笔强平事件、无付费数据商预算）决定这两项在本 spec 下不可达成,非缩窗量级的裁量,是结构性不可得。V5（危机窗行为判据,§7.2）相应改判 **N/A**，不计入 PASS/REJECT。

**FAIL 规则**：G0.1–G0.5 任一持续性未过 → Phase 0 FAIL，停机上报覆盖矩阵。**新增 INSUFFICIENT_DATA 判据**：若滚动窗口内 T1∧T2 触发episode 数不足以支撑 §7.2 V3(N_trades≥100)，Phase 0 本身可通过（数据质量本身合格）但下游判决锁定为 INSUFFICIENT_DATA，需等待更多自然发生的瀑布事件积累——参考同类型阈值（T1 用 5m 滚动名义额 p99）,预期是月级而非日级的积累周期，需向 Wiki 如实报告预期等待时长，不得用"数据在流入"掩盖"样本不足"。

## 5. 信号定义（预注册，进管线前锁定）

### 5.1 标的与时序纪律（Phase R 后修订：Binance → OKX）
**BTC-USDT-SWAP（OKX，USDT 本位永续）**，1m 闭合 K 线。原 spec 指定 Binance BTCUSDT；Phase R
确认 Binance 在本机无可用的强平采集路径（forceOrder geo-blocked、官方历史归档不含 USDT 本位
逐笔强平事件），而 OKX 全链路（强平/funding/OI/orderbook/OHLCV 含 1m）已在 `iris` 跑通,
故换标的，非换假设——H0 本身venue-agnostic（"多空对称"不依赖交易所）。全部滚动统计仅使用
t-1 及更早数据；信号在 t 闭合后评估，t+1 执行。做空为做多的完全镜像（空头爆仓 → 上行超调 →
衰竭后做空）。

### 5.2 触发（做多方向表述）
- **T1 强度**：多头爆仓 5m 滚动名义额 > 该统计量自身过去 30d 分布的 p99（按 1m 步进滚动样本计算分位）
- **T2 位移**：瀑布窗内峰谷位移 > 2 × ATR(60, 1m)，方向向下
- **瀑布窗**：自 T1 首次满足起，至衰竭确认或 60m 超时

### 5.3 衰竭确认（入场许可）
- **E1**：最近 1m 多头爆仓额 < 本次瀑布峰值 1m 额的 30%
- **E2**：OI 5m 变化 ≥ 0（停止收缩）
- E1 ∧ E2 首次同时成立的闭合 1m → 进入入场流程；60m 内未确认 → 本次作废（写 decision_trail）

### 5.4 确定性 regime 过滤（v1，替代 HMM）
- **F1 顺势崩溃跳过**：|24h 收益| > 10% 且瀑布方向与 24h 趋势同向 → skip
- **F2 连锁瀑布跳过**：过去 6h 内已出现 ≥ 3 次 T1∧T2 触发 → skip
- 所有 skip 必须写 decision_trail（含原因码）

### 5.5 执行模型
- **C0 主配置**：post-only maker 限价挂信号侧最优价，TTL 30s，未成交撤单放弃，不追价
- **maker 成交判据（保守，预注册）**：仅当后续 K 线价格严格穿越限价（做多：low < 限价）才判成交；触及不算
- **C1 对照**：taker 市价；滑点模型 = 半价差 + 2bp 固定冲击（保守初值，写入 fingerprint）
- **成本**：费率表参数化，当前档位与 BNB 抵扣状态由 Phase R 落实并写入 fingerprint；持仓跨 funding 时点按实际 funding 计提

### 5.6 离场
- **TP**：位移 50% 回撤 或 触及当日（UTC 日界）session VWAP，先到先出
- **SL**：瀑布极值外 0.25 × ATR
- **时间止损**：90m
- **同 bar 冲突裁决（预注册，防美化）**：同一 1m 内 SL 与 TP 均可触 → 记 SL

### 5.7 仓位与风控（策略内生，非外挂）
- 单笔风险 25bp 权益，按 SL 距离反推名义仓位；同时最多 1 仓
- 日内停机：3 连败 或 日内 -1.5% → 当日 flat，次一 UTC 日重置

## 6. 预注册配置预算（全集 = 8，DSR trials = 8，不得追加）

| ID | 变体 |
|---|---|
| C0 | 基准（maker） |
| C1 | taker 对照（降级测试用） |
| C2 | 触发分位 p99 → p97 |
| C3 | 衰竭比例 30% → 50% |
| C4 | TP 回撤 50% → 62% |
| C5 | SL 0.25 → 0.5 × ATR |
| C6 | 时间止损 90m → 45m |
| C7 | 过滤器消融（F1、F2 关闭） |

规则：配置选择只发生在 C0–C7 内。±30% 参数扰动网格（对 T1 分位、T2 倍数、E1 比例、TP/SL 参数）为选后稳健性检验——只判期望收益符号稳定性，禁止据此更换配置。任何超出 8 配置的执行 = 预算耗尽，停机上报。

## 7. 验证与判决

### 7.1 协议
helios_backtest_v51；CPCV purged K-fold；purge = 事件持仓重叠期；embargo ≥ 24h（> 最大持仓 90m + 特征回看余量）。

### 7.2 判据（V1–V6 全过 = PASS）
- **V1**：DSR > 0（trials = 8 硬编码入计算；PSR 一并报告）
- **V2**：费后 PF：maker 路径 ≥ 1.5；C1 taker ≥ 1.2
- **V3**：主窗口 N_trades ≥ 100，否则判 INSUFFICIENT_DATA（不是 PASS）
- **V4**：±30% 扰动内期望收益不翻号
- **V5**：~~危机窗行为——FTX-2022 与 COVID-2020~~ → **N/A（Phase R/§4 已确认无法获取该历史区间数据，Wiki 已裁决撤销此判据，不计入 PASS/REJECT，不得视为自动通过）**
- **V6**：C7 消融必须劣于 C0，否则过滤器无效，报告为设计缺陷

### 7.3 判决字典
PASS / REJECT / INSUFFICIENT_DATA / BLOCKED(P0)。REJECT 报告中不得出现"调参续命"建议；如需重启，走新 spec 与新预算。

## 8. 3O 合规映射（Phase R 后修订：对齐实际 v3.0 规范）

- **oskill（纯函数，golden vector 单测，无 IO）**：滚动爆仓和、滚动分位阈值、ATR、位移测量、衰竭标志、session VWAP、F1/F2 过滤标志。命名遵循实际仓库约定——裸 snake_case，置于 `oskill/oskill/` 目录，`from oskill import xxx` 导入，**无** `oskill_` 前缀（spec v1.0 假设有误，已订正）。
- **omodul 四支柱**（v3.0 规范最低要求 ≥1 个，本 spec 主动选择全部 4 个）：fingerprint（数据快照 hash、参数集 ID、代码版本、费率表）；decision_trail（每次触发/作废/skip 的输入-阈值-决定全记录）；report（episode 级统计 + 判决明细）；cost（计算与 API 成本）。文件同样裸 snake_case 置于 `omodul/omodul/` 目录，无 `omodul_` 前缀。真实 4 支柱范例参考 `omodul/omodul/helios_workflows.py`（v3.0 spec §10 自引的 `portfolio_optimization_workflow.py` 实际不存在于仓库，不要照抄该引用）。
- **service 层**（调度、落库）在 3O 之外，遵守 v3.0 边界
- 命名以 Phase R 确认的仓库约定为准（见上）；与本 spec 冲突时上报裁决，不得自行改动

## 9. 验收清单（DoD）

- [x] Phase R 勘察报告（含与 spec 假设的冲突清单）—— 完成于 2026-07-08，见 §12
- [ ] Phase 0 审计报告：G0.1–G0.5 全绿（G0.6 为滚动覆盖记录，非 FAIL 判据），或 FAIL 上报 + 覆盖矩阵；数据积累未达 §7.2 V3 门槛前锁定 INSUFFICIENT_DATA
- [ ] oskill 单测全绿，含 golden vectors 与前视泄漏专项测试（对滚动统计做 t+1 数据扰动不变性检验）
- [ ] omodul 四支柱验收：任意一次运行可由 fingerprint 完整复现
- [ ] 8 配置 CPCV 全部跑完 + 扰动网格
- [ ] 判决报告落库：V1–V6 明细（V5 = N/A 并说明理由）、全部 commit hash
- [ ] merged to main；"complete" 定义 = 合入 main，本地绿不算

## 10. ADR / 规范锚点（Phase R 后修订）

- `platform/3O/HELIOS_3O_SPEC_v3_0.md`（实际当前版本 v3.0，非 v1.0 假设的 v0.7）—— 3O 结构、四支柱、service 边界
- `platform/3O/helios_backtest_v51/`（含 `cpcv.py`、`stats/psr.py`、`stats/dsr.py`）—— CPCV/PSR/DSR 协议，Phase R 确认实际实现且已验证
- 既有验证红线（沿用不新造）：费后 PF ≥ 1.5、DSR/PSR、CPCV purge + embargo
- ~~coinalyze collector 缺陷与修复文档~~ —— Phase R 确认不存在，条款已删除（见 §3/§12）
- HMM regime omodul 缺陷记录：改引用 helivex commit `646dc71`、`docs/R11_HMM_REGIME_COMBO_GATE.md`、`docs/R12_XASSET_REGIME_GATE.md`（真实、已验证的 11/11 FAIL 记录，取代 v1.0 中查无实据的"三缺陷"表述）

## 11. 决策标记（Phase R 后修订）

**【锁定】** 触发/衰竭/离场初值、maker 保守成交判据、同 bar SL 优先、8 配置预算、trials = 8、Phase 0 阻断权
**【已改，Wiki 已批准,2026-07-08】** 主窗口边界（撤销 24 个月/双危机窗强制，改滚动正向窗口）、标的（Binance BTCUSDT → OKX BTC-USDT-SWAP）
**【可改，须 Wiki 批准并重新预注册】** taker 滑点模型、25bp 单笔风险、F1 的 10% 阈值

## 12. Phase R 勘察结论与决策记录（2026-07-08）

### 12.1 与 v1.0 假设不符之处（逐条,全部已裁决）

| # | v1.0 假设 | Phase R 实际发现 | 裁决 |
|---|---|---|---|
| 1 | coinalyze 聚合爆仓为主源 | 不存在——本机基础设施里 coinalyze 只在无关的 `helios` 项目出现,且只采 OI/funding,从未采过爆仓 | 改用 OKX（`iris` `stream-okx`），见 §3/§5.1 |
| 2 | "00:00 UTC 桶"爆仓时间戳 bug 已修复 | 查无此 bug 或修复记录（因为对应的 collector 本身不存在） | G0.1 判据保留但重新定性为一般性质量校验，不再绑定"已修复"叙事 |
| 3 | Binance forceOrder 作交叉校验 | 在本机 geo-blocked，`helios` 项目已放弃，无功能实现 | 交叉校验判据（G0.2 后半）取消 |
| 4 | Binance funding/OI/1m OHLCV，24 个月+双危机窗覆盖 | Binance 官方历史归档（data.binance.vision）：1m K 线/funding 确实可回溯到 2019-12-31/2020-01（免费），但 **OI（metrics）只到 2020-09-01（覆盖不到 COVID-2020）；USDT 本位 liquidationSnapshot 已被官方整体下架，coin 本位 liquidationSnapshot 只从 2023-06-25 起（两者都覆盖不到 FTX-2022/COVID-2020）** | 不采购付费数据商；不构造代理信号；撤销 24 个月/双危机窗强制要求，改为滚动正向采集（见 §4）。用户决策，非 CC 裁量 |
| 5 | HELIOS_3O_SPEC v0.7 | 实际当前版本 v3.0（`platform/3O/HELIOS_3O_SPEC_v3_0.md`，2026-06-12），v0.7 在任何仓库都不存在 | 全文引用改为 v3.0 |
| 6 | `oskill_xxx.py`/`omodul_xxx_signal` 命名前缀 | 实际约定：裸 snake_case，目录限定（`oskill/oskill/`、`omodul/omodul/`），无前缀 | 命名约定改用实际仓库约定，见 §8 |
| 7 | omodul 四支柱为平台强制要求 | v3.0 规范只要求 ≥1 个，非强制全 4 个 | LER 主动选择全部 4 个（原设计不变，仅措辞订正） |
| 8 | 引擎 = helios_backtest_v51 | 确实存在且正确实现 CPCV/PSR/DSR，但**不是 helivex 当前实际生效的判决引擎**——真正在用的是 `helivex/tools/strategy_gate.py`，其自身承认是非纯化/非组合式的历史遗留启发式 | 采用 `helios_backtest_v51`（符合 v1.0 原意，且是更严谨的实现），非 `strategy_gate.py` |
| 9 | HMM regime omodul 三个复合缺陷 | 查无此文档/记录，最接近的是完全不同的一个非 HMM 分类器 | 排除 HMM 依赖的依据改引用真实证据：commit `646dc71`、`docs/R11_HMM_REGIME_COMBO_GATE.md`、`R12_XASSET_REGIME_GATE.md`（11/11 FAIL） |
| 10 | "Bloch REJECT" 判决先例 | 查无此记录，helivex 门历史只用 PASS/FAIL 术语，从未出现 REJECT 或 Bloch 字样 | §4 引言删除该引用 |

### 12.2 Wiki 决策记录

- **2026-07-08**：数据源可重新设计（用户批准，不采购付费数据商）；上述 #5–#10 由 CC 裁定（用户授权"你定"）；#4 的具体处理方式（放弃深史，改滚动正向采集，不构造代理信号）由用户直接拍板。
- **配套基础设施变更**（本次 Phase R 期间一并完成，非 spec 编码范围，但是 §3 数据源重定向的前置）：
  - `iris` repo `feat/p2a-ohlcv` 分支 commit `93ba851`：补 OKX `m1/m15/h1/d1` OHLCV 采集（原为未提交 WIP，本次验证 66/66 测试通过后提交）+ OKX 现货 h1（不接消费方，仅验证）。
  - `iris` repo commit `18bb3e9`：修复上一提交引入的 `BinanceVenue.ohlcv()` 缺少 `instrument_type` 形参导致的 `ohlcv-binance-h4/m5` 采集器崩溃（部署时发现，同一会话内修复并重新部署）。
  - 两次 commit 均已 `docker compose build && up -d iris`，部署后确认 `md.ohlcv` 已有 OKX m1 数据流入，无 collector 报错。

### 12.3 遗留待办（非本次 Phase R 范围，进入 Phase 0 前需注意）

- V3（N_trades ≥ 100）在纯正向滚动积累下，预期是月级时间尺度，需要向 Wiki 如实报告实际等待进度，不得以"数据在流入"代替"样本充足"。
- G0.6 覆盖矩阵报告应逐周更新，作为判断"何时数据量足以启动 Phase 0 正式判决"的依据，而非一次性产出。
