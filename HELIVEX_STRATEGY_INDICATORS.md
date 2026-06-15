# HELIVEX Strategy Indicators System (R7)

Config-driven indicator framework: 3 strategies × 6 indicators, YAML-configurable, every config gates.

---

## §1 Indicator Layer (oprim)

All TA indicators are atomic, stateless, keyword-only params, pandas Series passthrough.

### Existing (pre-R7)
| Function | Location | Output |
|----------|----------|--------|
| `ema(prices, *, window)` | `oprim.technical.moving_averages` | series |
| `sma(prices, *, window)` | `oprim.technical.moving_averages` | series |
| `vwap(prices, volumes, *, window)` | `oprim.technical.moving_averages` | series |
| `macd(prices, *, fast_period, slow_period, signal_period)` | `oprim.technical.moving_averages` | dict {macd, signal, histogram} |
| `rsi_normalized(prices, *, period)` | `oprim.technical.oscillators` | series [0,1] |
| `stochastic_oscillator(h,l,c, *, k_period, d_period, smooth_k, normalize)` | `oprim.technical.oscillators` | dict {k, d, raw_k} |
| `bollinger_bands(prices, *, window, num_std)` | `oprim.technical.bands` | dict {upper, middle, lower, bandwidth, percent_b} |
| `donchian_channel(highs, lows, *, window)` | `oprim.technical.bands` | dict {upper, lower, middle} |
| `keltner_channels(h,l,c, *, ema_period, atr_period, multiplier)` | `oprim.technical.bands` | dict {upper, middle, lower} |
| `obv(closes, volumes)` | `oprim.technical.volume` | series |

### Added in R7 (`oprim/technical/trend.py`)
| Function | Location | Output |
|----------|----------|--------|
| `atr_series(h,l,c, *, period=14)` | `oprim.technical.trend` | series (Wilder smoothing) |
| `adx_series(h,l,c, *, period=14)` | `oprim.technical.trend` | dict {adx, plus_di, minus_di} — all series |
| `supertrend(h,l,c, *, period=10, multiplier=3.0)` | `oprim.technical.trend` | dict {direction, upper_band, lower_band, line} |

ADX > 25 = trending. SuperTrend direction: +1 = uptrend (support active), -1 = downtrend.

---

## §2 Compose Layer (oskill)

Config-driven confluence logic. Stateless. Returns `np.ndarray[int8]` of +1/0/-1 signals.

### `trend_signal_compose` (`oskill/trend_compose.py`)
Uses: SuperTrend + EMA crossover + ADX + MACD

Config schema:
```yaml
indicators:
  supertrend: {enabled: bool, period: int, multiplier: float}
  ema:        {enabled: bool, fast: int, slow: int}
  adx:        {enabled: bool, period: int, threshold: float}  # ADX > threshold = trending
  macd:       {enabled: bool, fast: int, slow: int, signal: int}
signal_logic:
  min_confluence: int    # N indicators must agree
  direction: both|long|short
```

### `mean_reversion_compose` (`oskill/mean_reversion_compose.py`)
Uses: VWAP z-score + Bollinger Bands + RSI + Stochastic

Config schema:
```yaml
indicators:
  vwap:       {enabled: bool, window: int, z_threshold: float}
  bollinger:  {enabled: bool, window: int, num_std: float}
  rsi:        {enabled: bool, period: int, oversold: float, overbought: float}
  stochastic: {enabled: bool, k_period: int, d_period: int, smooth_k: int, oversold: float, overbought: float}
signal_logic:
  min_confluence: int
  direction: both|long|short
```

Both functions: indicators not mentioned in config are disabled. `enabled: false` also disables.

---

## §3 Strategy Layer (omodul)

Three strategy modules in `omodul/strategies/`:

| Module | Function | Compose | Timeframe | Direction |
|--------|----------|---------|-----------|-----------|
| `trend_dual.py` | `trend_dual(market_state, config)` | `trend_signal_compose` | 4H SWAP | long+short |
| `vwap_mr_dual.py` | `vwap_mr_dual(market_state, config)` | `mean_reversion_compose` | 1H SWAP | long+short |
| `spot_trend.py` | `spot_trend(market_state, config)` | Donchian or `trend_signal_compose` | 1D spot | long-only |

All return: `{signals, n_signals, cost_bps, audit_evidence}`.

`market_state` keys: `ohlcv` (dict with high/low/close/volume arrays), `instrument`, `current_positions`, `capital_usd`.

`spot_trend` Donchian mode: activated by `config['donchian']` dict. Entry = close[i] > max(close[i-n:i]) (shift=1, no lookahead). Bear filter: suppress entries when close < SMA(bear_ma).

---

## §4 YAML Config Schema (§4 reference)

```yaml
strategy: trend_dual | vwap_mr_dual | spot_trend
timeframe: 4H | 1H | 1D
instruments: [BTC-USDT-SWAP, ...]
db_table: market_data.ohlcv_1h
db_source: okx_swap | okx_swap_5m
resample_bars: int           # group N consecutive bars (1=no resample)
resample_to_1d: bool         # resample to daily via date groupby

# For spot_trend:
donchian:
  n_enter: int     # N-bar breakout window
  n_exit: int      # N-bar pullback exit window

indicators:        # compose indicator params (see §2)
  ...

signal_logic:
  min_confluence: int
  direction: both|long|short

risk:
  cost_bps: float      # per-RT cost (e.g. 10 = 5bps/side taker)
  bear_ma: int         # 0 = disabled
  hold_bars: int       # max hold for MR strategies (not enforced in backtest gate)

mode: backtest | paper

gate:
  n_splits: int         # CPCV folds
  embargo_bars: int     # bars to drop between IS and OOS
  pbo_threshold: float  # max allowed PBO
```

---

## §5 strategy_gate CLI

```bash
python tools/strategy_gate.py --config strategies/trend_dual.yaml [--instrument BTC-USDT-SWAP] [--quiet]
```

Gate sequence:
1. Load YAML → import omodul strategy function
2. Fetch OHLCV from TimescaleDB; resample if needed
3. Run strategy → per-bar signals
4. CPCV walk-forward (n_splits folds, embargo_bars dropped between IS/OOS)
5. Per-fold: first 2/3 IS, last 1/3 OOS → compute OOS Sharpe
6. DSR = mean(OOS) - std(OOS)  (penalize high variance)
7. PBO = fraction of folds where IS Sharpe > OOS Sharpe
8. Adjust DSR threshold for global trial count (Bailey & López de Prado 2016)
9. PASS if: DSR > dsr_threshold AND PBO < pbo_threshold
10. Write trial to `.gate_trials.json`

**Global trial counter**: `.gate_trials.json` in project root. DSR threshold increases with N:
- N=1: threshold=0.0
- N=5: threshold≈1.2
- N=10: threshold≈1.5
- N=20: threshold≈1.8

---

## §6 Baseline Gate Results (R7 defaults, Trial #1-5)

| Strategy | Instrument | Gross SR | Mean OOS | DSR | PBO | Verdict |
|----------|-----------|---------|---------|-----|-----|---------|
| trend_dual | BTC-USDT-SWAP | +0.449 | +1.229 | -0.536 | 0.17 | **FAIL** |
| trend_dual | ETH-USDT-SWAP | +0.803 | +0.022 | -1.158 | 0.67 | **FAIL** |
| trend_dual | SOL-USDT-SWAP | +0.901 | -0.394 | -1.796 | 0.83 | **FAIL** |
| vwap_mr_dual | SOL-USDT-SWAP | -0.833 | -0.463 | -1.552 | 0.67 | **FAIL** |
| vwap_mr_dual | BTC-USDT-SWAP | -0.923 | -2.338 | -4.240 | 0.83 | **FAIL** |
| spot_trend | BTC-USDT-SWAP | +1.330 | +1.323 | -0.742 | 0.17 | **FAIL** |
| spot_trend | ETH-USDT-SWAP | +1.348 | +0.811 | -0.537 | 0.67 | **FAIL** |
| spot_trend | SOL-USDT-SWAP | +1.082 | +0.241 | -1.634 | 0.67 | **FAIL** |

### Analysis

**trend_dual (4H SWAP)**: BTC shows promising mean OOS (+1.229) but DSR < 0 because fold variance is extreme: folds 1-4 strong (+2.9, +2.6, +3.1, +1.0) but folds 5-6 collapse (-1.4, -0.7). Root cause: EMA/MACD crossover with min_confluence=2 is always in a position → high frequency relative to 4H timeframe → cost-insensitive but regime-dependent. Wiki action: try higher min_confluence (3), or disable MACD and rely on SuperTrend+ADX.

**vwap_mr_dual (1H)**: Negative gross Sharpe for both instruments. The 4 indicator confluence at 1H is too restrictive and generates poor signals. The original R5.3 VWAP-MR used only VWAP z-score with 1-indicator logic. Wiki action: try `min_confluence: 1` with only VWAP enabled (replicates R5.3 baseline).

**spot_trend (1D)**: Gross Sharpe +1.330 (BTC) matches R6.0 exactly. FAIL on DSR due to fold variance (folds 1-4 good, folds 5-6 collapse to 0 due to bear filter blocking late-cycle entries). Same structural issue as R6.0. Wiki action: try without bear filter, or extend data to include pre-2020 history for more diverse folds.

These baselines establish the starting point. Configs are the variables — gate enforces each exploration is tracked.

---

## §7 Wiki Action Items (R8)

1. **Frontend config UI** (`ui/`) — slider/toggle for each indicator param
2. **trend_dual BTC variant**: min_confluence=3, disable MACD → re-gate
3. **vwap_mr_dual**: min_confluence=1, only VWAP → compare to R5.3 directly
4. **spot_trend**: try without bear_ma (bear_ma=0) to check if bear filter hurts more than helps in CPCV
5. **New indicator**: Chandelier Exit as trend filter for trend_dual
6. **Report trial N** in UI so user sees selection bias penalty growing
