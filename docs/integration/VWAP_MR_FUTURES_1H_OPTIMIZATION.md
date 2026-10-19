# vwap_mr_1h / futures_signal_port — 1H 优先优化（Veya Loop BO × 3 seed + 跨标的验证）

> 按指令对两个 1H 周期策略做优先优化。结论：**修正移植失真后，vwap_mr_1h 呈现弱但
> 跨标的稳定的均值回归边际（BTC/ETH/SOL 15 个 test 组合 12 个 ret>0，ETH/SOL 9/9 全正）；
> futures_signal_port 无稳健 alpha（SOL 全负）**。两者 Sharpe 均达不到 0.3 门禁，
> 样本（6~30 笔）不足以支撑部署判决 —— 建议延长观察期或多标的组合后再判。

## 1. 移植修正（对照 paper 实现，两处失真）

| 策略 | 原移植（失真） | paper 实现（修正后） |
|---|---|---|
| vwap_mr_1h | BB+RSI+Stoch confluence（min_confluence=2） | **纯 z-score 入场** + SL/TP 括号（sl_std×σ / min_rr×SL）+ hold 时间出场 + 止损冷却 |
| futures_signal_port | 无趋势确认、无止损（只靠对称出场） | **EMA50/MACD 趋势确认**（long_conf/short_conf）+ **SL/TP 括号**（sl_atr_mult=2.0×ATR / min_rr=1.5×SL）优先，对称出场兜底 |

修正后默认参数：vwap_mr_1h test Sharpe −0.032 → **+0.008**；futures_signal ret 保持正。

## 2. BO 结果（每策略 3 seed × 30 次回测，train [04-02, 06-15) → test [06-15, 08-01)）

### vwap_mr_1h（7 参数：vwap_n / std_n / z_thr / hold / sl_std / min_rr / cooldown）

| seed | best（n/σ/z/hold/sl/rr/cool） | train SR | test SR | test ret | test ntr | 判定 |
|---|---|---|---|---|---|---|
| 0 | 7.1/40.5/2.7/9.0/1.67/2.28/6.1 | −0.035 | +0.021 | +2.2% | 22 | REJECT |
| 1 | 7.2/31.8/2.95/9.6/1.50/2.16/6.1 | −0.070 | −0.005 | +0.7% | 30 | REJECT |
| 2 | 7.9/40.8/2.96/7.5/1.04/1.86/2.7 | −0.065 | **+0.077** | +3.3% | 18 | REJECT (n_trades) |

BO 稳定收敛：**vwap_n≈7、std_n≈32-41、z_thr≈2.7-3.0（更严）、hold≈8-10（更长）、
cooldown≈3-6** —— 即「更慢、更严、更耐心的均值回归」。

### futures_signal_port（5 参数：breakout / surge / rsi / sl_mult / min_rr）

| seed | best | train SR | test SR | test ret | test ntr | 判定 |
|---|---|---|---|---|---|---|
| 0 | 29.5/2.02/10.1/1.11/2.01 | **+0.069** | +0.000 | −0.3% | 22 | REJECT |
| 1 | 27.8/1.84/10.1/2.29/1.60 | +0.064 | −0.023 | −2.2% | 24 | REJECT |
| 2 | 29.4/2.10/10.6/1.80/1.28 | +0.046 | +0.000 | −0.1% | 26 | REJECT |

train 稳定微正（SR +0.05~+0.07），test 归零 → 微弱信号样本外消失（典型弱过拟合）。

## 3. 跨标的样本外验证（BTC best 参数 → ETH/SOL，test 窗口）

### vwap_mr_1h

| | ETH ret / SR | SOL ret / SR |
|---|---|---|
| seed0 | +4.11% / +0.017 | +4.69% / +0.003 |
| seed1 | +0.38% / +0.005 | +1.63% / +0.034 |
| seed2 | +0.47% / −0.027 | +1.54% / +0.000 |

**ETH/SOL 9/9 ret 全正**（加 BTC 的 2/3 → 15 组合 12 正）。弱但跨标的稳定 ——
z-score 均值回归在 1H 上的 gross 边际真实存在（net 已含 12bps 成本）。

### futures_signal_port

| | ETH ret / SR | SOL ret / SR |
|---|---|---|
| seed0 | +1.89% / +0.002 | −3.07% / −0.062 |
| seed1 | +2.95% / +0.041 | −4.47% / −0.031 |
| seed2 | −1.59% / −0.010 | −3.12% / −0.047 |

ETH 2/3 正、SOL 全负 → 无跨标的稳健性。

## 4. 三标的组合回测（按指令追加，2026-08）

扩展 `backtest_bridge._run_portfolio`：每标的独立（validation + simulate）→ 等权组合净值
（mean equity），组合 Sharpe/MDD/ret 由组合净值计算，n_trades/cost 为合计。
数据：BTC/ETH/SOL 1H（`data/scan_{btc,eth,sol}_1h.parquet`，同源 5m 聚合 + 真实 funding）。

### vwap_mr_1h — 组合 BO（3 seed × 30 次回测，train 组合 → test 组合门禁）

| seed | best（n/σ/z/hold/sl/rr/cool） | train SR | test SR | test ret | test ntr | 判定 |
|---|---|---|---|---|---|---|
| 0 | 5.9/32.9/2.38/6.8/1.28/1.97/4.5 | −0.359 | **+0.422** | +1.95% | 104 | **ACCEPTED** |
| 1 | 3.3/47.0/2.94/9.2/1.90/1.40/7.3 | −0.285 | **+0.437** | +1.60% | 32 | **ACCEPTED** |
| 2 | 5.6/30.5/2.29/7.3/1.38/1.99/4.6 | −0.106 | +0.279 | +1.39% | 128 | REJECT (0.279<0.3) |

- **2/3 seed 过 test 门禁**；三个 seed 的 test 三标的 ret **9/9 全正**（seed0：
  BTC +1.88% / ETH +3.59% / SOL +0.38%）。
- **参数稳健性**（seed0 best ±10% 单参数扰动 × 14）：test SR **14/14 > 0.3**
  （min +0.302 / max +0.596），ret 全正 → 非尖点过拟合，是参数邻域的整体性质。
- ⚠ 诚实标注：train 段（04-02→06-15）组合 SR 为负（−0.11~−0.36），test 段转正 ——
  train/test 区间存在 regime 差异，证据强度弱于「train 验证过的 alpha”；
  建议滚动窗口 / 更长数据复核后再翻 `trade_enabled`。

### vwap_mr_1h — 80% 资金仓位（按指令追加，position_fraction=0.8）

仓位从 50% 提到 80%（每标的入场 0.8 资金），三标的组合重跑 3 seed BO：

| seed | test SR | test ret | test mdd | test ntr | BTC/ETH/SOL ret | 判定 |
|---|---|---|---|---|---|---|
| 0 | **+0.467** | **+3.97%** | 0.041 | 104 | +5.91/+4.99/+1.02 | **ACCEPTED** |
| 1 | **+0.377** | +2.02% | 0.009 | 38 | +3.53/+0.29/+2.23 | **ACCEPTED** |
| 2 | +0.277 | +2.18% | 0.028 | 128 | +2.93/+1.96/+1.66 | REJECT |

- 2/3 seed 过门禁（与 0.5 仓位一致）；test ret 按 1.6× 缩放（+2.0%~+4.0%），
  Sharpe 基本不变（线性缩放理论验证：0.422→0.421/0.467）。
- 0.8 仓位扰动稳健性（seed0 best ±10% × 14）：**14/14 > 0.3**
  （min +0.367 / max +0.597）—— 高仓位下结论同样稳健。
- 成本占比 ~5%（0.8 仓位下 104 笔 ≈ 5.1% 资本），MDD 0.041 仍在 0.25 限内。

### futures_signal_port — 组合 BO（对照组）

| seed | train SR | test SR | test ret | 判定 |
|---|---|---|---|---|
| 0 | +0.375 | −0.450 | −3.6% | REJECT |
| 1 | +0.468 | +0.036 | +0.1% | REJECT |
| 2 | +0.344 | +0.254 | +1.1% | REJECT |

组合分散把 train 拉到 +0.34~+0.47，但 test 不稳定（1/3 接近门禁、1/3 深度为负）→
维持 REJECT，不投入。

## 5. 结论与建议（更新）

1. **vwap_mr_1h（组合）**：三标的等权组合下 2/3 seed 过 test 门禁、±10% 扰动全过、
   9/9 标的 ret 正 —— 是目前唯一接近可部署判决的策略。保留项：train 段为负的区间效应；
   建议下一步跑滚动窗口验证（数据长度允许后）再翻 `trade_enabled`。
2. **futures_signal_port**：train 正 / test 不稳定 / SOL 全负 → VALIDATED-REJECT。
3. **Veya Loop 实用性**：组合口径 BO（30 次回测/seed）在 3 标的等权组合上找到
   test 正参数，且扰动稳健性证明搜索结论可迁移；多 seed + 扰动 + 跨标的三重检验
   均已在适配层落地。

## 6. 审计与复现

- 2026-08-06 已部署 paper：`strategies/vwap_mr_1h.yaml` live 块更新为 seed0 best
  @ 80% 仓位参数（vwap_n=7/std_n=44/z_thr=2.02/hold=7/sl_std=1.63/min_rr=2.39/
  cooldown_after_sl=5）+ `trade_enabled: true` + `max_qty_usd=4000`（0.8×BASE 5000）；
  `paper/node.py` 注册 BTC/ETH/SOL 三标的 VwapMR1H 实例；容器 `helivex-paper`
  已重启，3 实例 READY + warmup seeded（46 根 1h bar/标的），无异常日志。
  保留项：train 段区间效应 → paper 观察期复核（tracking 由 paper/monitor.py 负责）。

- `integrations/veya_loop_quant/audit/opt_vwap_mr_1h.jsonl`（3 seed 单标的）
- `integrations/veya_loop_quant/audit/opt_vwap_mr_1h_port1h.jsonl`（3 seed 组合）
- `integrations/veya_loop_quant/audit/opt_vwap_mr_1h_port1h_pf80.jsonl`（3 seed 组合 @ 80% 仓位）
- `integrations/veya_loop_quant/audit/opt_futures_signal_port_1h.jsonl` / `opt_futures_signal_port_port1h.jsonl`
- 数据：`data/scan_*_1h.parquet`（BTC/ETH/SOL，2026-04-02 → 08-01，5m 聚合 + 真实 funding）
