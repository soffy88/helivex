# HELIXA 退役清单(P10)

版本 v1.0 · 2026-07-09 · 依据:helixa 的决策核心已由 helivex 用 3O 范式完整重建(P1-P9),且**每一层带真门禁**(helixa 的门从未生效)。本清单给出"可以关 helixa 哪些、必须留哪些、为什么",以及执行与回滚。

## 诚实前提(不夸大)

- helivex 与 helixa **都是 paper/sandbox,均不涉及真实资金**。"替代"指:helivex 在**能力上是超集**、在**门禁纪律上更强**,可停掉 helixa 的冗余决策层。
- helivex 的共识→执行目前是 **observe-only**(`HELIVEX_CONSENSUS_ENFORCE=observe`),不下 paper 单。helixa 的 nautilus 也是 SandboxExecutionClient(不接真交易所)。两者都不产生真实订单,故停 helixa 决策层无实盘影响。
- **数据层归 iris**(采集层合并已完成),情绪/链上也已进 iris。helixa 的数据 adapter 是"helixa 消费 iris",停不停都不影响 iris 生产。

## 能力映射(helixa → helivex 3O,均已完成)

| helixa 服务 | helivex 替代 | 门禁增强 |
|---|---|---|
| prob-engine(多引擎共识+权重学习) | consensus_adapter(P5)+ ewma_weight_update | 只 promoted 引擎驱动(helixa 什么都投) |
| risk-engine-spot/futures(3层裁剪+熔断+crisis+fee/edge) | cvar_risk_adapter(P1)+ consensus_risk_size(P6) | 同口径 + 四档熔断(比 helixa 两态强) |
| portfolio-optimizer(CVaR) | cvar_risk_adapter(P1) | observe→enforce 分阶段 |
| regime-detector(HMM) | regime_adapter(P2) | advisory + 确定性主(HMM 无 OOS 价值,helivex 自证) |
| qlib-v2(ML 信号) | signal_engines_adapter ml_lgb(P4) | **DSR 门真生效**(helixa VALIDATION_STRICT=false 从未生效) |
| tradingview-engine(TA) | signal_engines_adapter ta_multi(P4) | 过 P5 共识 + observe |
| ai-hedge-fund/finrobot/tradingagents(LLM) | llm_signal_workflow 槽(P4) | 默认禁用零成本(helixa 生产权重也是 0) |
| factor-analyzer(DSR+归因) | ml_signal_workflow DSR + engine_attribution(P6) | 归因闭环喂 EWMA |
| public-api(只读 API) | gateway 端点(/regime /engines /consensus /risk_eval + P1) | token + loopback + cloudflared |
| dashboard(前端) | helivex-web Ensemble tab(P8) | 同源 + Basic Auth |
| watchdog(健康检查) | eval_ensemble_freshness(P9) | 进现有 AlerterEngine |
| tg-notifier(事件推送) | eval_regime_switch(P9) | 同 TG 通道 |
| 情绪/链上采集 | iris md.sentiment/md.onchain(P3) | 数据层归 iris |

## ⛔ 必须保留(iris/共享依赖,**不可停**)

- **quant-proxy-relay**(10808 隧道)——**iris 的 OKX 采集依赖它**(`iris OKX_PROXY=host.docker.internal:10808`)。停了 iris 拿不到 OKX 数据,helivex 全链路断。**绝对保留。**
- **quant-data**(Binance 现货 OHLCV)—— iris 不采 Binance 现货,这是 helixa 独有数据源,停了会丢 Binance 现货历史。保留(低成本)。
- **quant-rabbitmq** / **helios-quant-subscriber** —— 共享基础设施,保留。
- helixa 的 md-adapter(quant-{ohlcv,derivatives,funding-oi-history,binance-funding,taker-flow}-md-adapter)—— helixa 消费 iris,与 helivex 无关,停不停皆可;本清单**保留**(它们只是 DB→DB,近乎免费,且是 helixa 侧自己的数据)。

## ✅ 可停(决策层,helivex 已替代)

本次(P0 勘察时)启动的决策服务,helivex 3O 已覆盖,可退回停止态:

```
quant-prob-engine quant-risk-spot quant-risk-futures
quant-nautilus-spot quant-nautilus-futures quant-portfolio-opt
quant-regime quant-public-api quant-tg-notifier quant-watchdog
quant-redis-bridge quant-risk-webhook
```

## 执行

```bash
cd /data/soffy/projects/helixa
docker compose -f docker-compose.v3.yml stop \
  prob-engine risk-engine-spot risk-engine-futures \
  nautilus-spot nautilus-futures portfolio-optimizer \
  regime-detector public-api tg-notifier watchdog \
  redis-bridge risk-event-webhook
# 关键:不碰 proxy-relay / data / rabbitmq / *-md-adapter
```

## 回滚

```bash
# 恢复某个服务:
docker compose -f docker-compose.v3.yml up -d <service>
# 或全恢复决策层(同 P0 enable 的清单)
```

## 停后验证(必须做)

1. `docker ps | grep quant-proxy-relay` —— 仍 Up(隧道活着)。
2. iris OKX 仍在采:`SELECT max(ts) FROM md.trades WHERE venue='okx'` 应持续更新。
3. helivex 全链路仍跑:5 个 adapter timer active,paper 4 策略 Up,`/consensus` 有数据。

## 未做(明确)

- **edge 式响应脱敏中间件**(helixa edge worker):gateway 已 loopback + token + cloudflared,且 P2-P6 端点不含密钥,故**评估为非必需,本期不实现**。如需对外公开只读 API,再加。
- **接 enforce(共识真下 paper 单)**:独立于退役,需人工放行 + observe 观察期(同 P1 Stage B),不在本清单范围。
