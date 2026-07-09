# HELIXA → HELIVEX 3O 全替换计划

版本 v1.0 · 2026-07-09 · 目标:helivex 用 3O 范式实现 helixa 的**每一项**能力,达成功能超集后停用 helixa。原则:**能力对齐,但每一项都接真门禁**(helixa 缺的纪律),使 helivex 是严格超集——功能不少,且更诚实、更强。

## 架构判断(定调)

helivex **不照搬** helixa 的微服务 + RabbitMQ 架构,只复刻**能力**。映射规则:
- RabbitMQ 事件总线 → helivex 的 **Postgres 表 + systemd 定时轮询**(helivex 无 Redis/MQ,现有 md-adapter 已是此模式,更简单)
- 每个"引擎/计算" → 一个 **oskill/omodul**(纯计算) + 一个 **服务层 adapter**(调度+落库)
- helixa 已有等价物的:gateway(≈public-api)、helivex-web(≈dashboard)、paper/alerter.py AlerterEngine(≈watchdog)、Telegram 告警(≈tg-notifier)、helios_backtest_v51 DSR(≈factor-analyzer DSR)

## 能力总表(helixa 全量 → 3O 映射 → phase → 状态)

| # | helixa 能力 | 3O 层 | helivex 落点 | Phase | 状态 |
|---|---|---|---|---|---|
| 数据 | OHLCV/funding/OI/orderbook/trades/liq | (iris 生产)| md-adapter | — | ✅ 已合并到 iris |
| 风控 | CVaR 组合优化 | oprim+oskill+omodul | cvar_risk_adapter | **1** | ✅ 完成(observe) |
| 风控 | 3 层仓位裁剪 + 熔断 | oskill+omodul | 同上 | **1** | ✅ 完成 |
| 风控 | crisis-override + fee/edge + 共识→风控桥 | oprim+oskill | consensus_risk_adapter | **6** | ✅ 完成(observe) |
| regime | 市场状态 crisis/trend/range(确定性主+HMM可选)| oskill+omodul | regime_adapter | **2** | ✅ 完成(advisory) |
| 数据 | 情绪 fear-greed(Alt.me FGI)| **iris collector**(数据层归 iris)| md.sentiment | **3** | ✅ 完成 |
| 数据 | 链上(Coin Metrics 免费,BTC/ETH)| **iris collector** | md.onchain | **3** | ✅ 完成 |
| 信号 | FGI→情绪偏置 + 链上→tanh signal | oskill 合成 | consensus_adapter | 5 | ✅ 完成 |
| 信号 | 多周期 TA(自研指标)| oskill(纯 TA)+adapter | signal_engines_adapter | **4a** | ✅ 完成 |
| 信号 | ML(LightGBM+三重障碍+WFV+DSR门)| oprim+oskill+omodul | signal_engines_adapter | **4b** | ✅ 完成(门拒绝) |
| 信号 | LLM 人格槽 | omodul | signal_engines_adapter | **4c** | ✅ 建槽(禁用零成本) |
| 共识 | 多引擎融合(只 promoted 驱动)| oskill+omodul | consensus_adapter | **5** | ✅ 完成 |
| 共识 | weight-learner(EWMA)| oskill | consensus_adapter | **5** | ✅ 机制就位 |
| 归因 | 引擎 round-trip P&L 归因 + 权重学习闭环 | oskill | consensus_risk_adapter | **6** | ✅ 机制就位(待标签成交)|
| 执行 | nautilus 2 策略(trend_follower/scalper_v2)| 策略逻辑纯函数 | helivex paper 策略 | **7** | ⬜ 可选 |
| API | 端点(regime/engines/consensus/risk_eval + P1)| gateway | 已随 P2-P6 建 | **8** | ✅ 核心完成 |
| 前端 | Ensemble tab(regime/引擎/共识/风控全链路)| helivex-web | EnsembleTab | **8** | ✅ 完成 |
| 运维 | 3O 管线健康检查 | evaluator | eval_ensemble_freshness | **9** | ✅ 完成 |
| 运维 | regime 切换事件推送 | evaluator | eval_regime_switch | **9** | ✅ 完成 |
| 运维 | 引擎权重/归因面板 | helivex-web | EnsembleTab 权重卡 | **8** | ✅ 权重已画(归因待成交)|
| 安全 | edge 脱敏 | (可选)| — | **10** | ⏸ 评估非必需(见退役清单)|
| — | RabbitMQ / redis-bridge | — | **不复刻**(表+轮询替代)| — | N/A |
| 退役 | helixa 决策层停机 | — | HELIXA_DECOMMISSION.md | **10** | ✅ 已执行 |

## Phase 顺序与依赖

- **P1 CVaR+动态风控** ✅ 完成(Stage A 观察中)
- **P2 Regime(gated HMM)** — 基础层,被共识/风控消费。复用 `oskill.hmm_regime_detect`。helivex 自己已证 HMM regime 无 OOS 持续性(commit 646dc71,11/11 FAIL)→ 输出作为**咨询性**信号 + 确定性 fallback,不当硬依赖。
- **P3 情绪 + 链上采集** — 免费外部 API,新增 md 风格表。喂共识。
- **P4 信号引擎** — 4a 多周期 TA(免费)、4b ML(免费算力,接真门禁)、4c LLM(付费,可选)。各引擎写 signals 表。
- **P5 多引擎共识 + 归因 + 权重学习** — "大脑",融合 helivex 自己 4 策略 + 新引擎。依赖 P2/P3/P4。
- **P6 风控补全** — crisis-override + fee/edge,扩展 P1;接共识→定仓信号。
- **P7 执行策略**(可选)— 移植 2 个 nautilus 策略。helivex 已有 4 策略,此为增量。
- **P8 API + 前端对齐** — gateway 补端点 + helivex-web 补视图(共识/引擎投票/regime/决策轨迹/K线图/归因)。
- **P9 运维对齐** — watchdog 检查 + tg 事件推送。
- **P10 安全 + 切换** — edge 式脱敏(可选)+ helixa 退役清单。

## 关键纪律(每 phase 都遵守)

1. **接真门禁**:qlib/LLM/共识信号,凡产出交易信号的,必须过 helios_backtest_v51 的 CPCV/DSR 门(helixa 的 `VALIDATION_STRICT=false` 从未生效——这正是 helivex 要更强的地方)。未过门的引擎输出只记录、不驱动实盘。
2. **observe→enforce 分阶段**:凡影响实盘 paper 决策的,先 observe-only 落库+前端可见,人工复核后再接 enforce(同 P1 的 Stage A/B)。
3. **诚实空状态**:数据不足/未验证一律明示,不用 mock 填充。
4. **surgical**:helivex 现有 4 策略与风控在每 phase 保持可运行,回归可查。

## 待裁决(见对话)

- **共享库策略**:platform/3O 被多项目共享且外部会切分支(P1 曾因此断过)。当前策略:提交到各库 main(与 omodul 一致),被切走则 cherry-pick 回。可选:helivex pin 到具体 commit,或 vendor 副本。
- **付费/未验证能力范围**:LLM 三件套(持续烧钱)、qlib ML(helixa 实测 0.2315,比随机差)——是否要 helivex 也具备(建槽+接真门禁,不照抄坏模型),还是本期跳过。
