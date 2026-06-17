/**
 * helivex mock 数据。
 * 诚实背景:三策略 backtest 均未过 gate(DSR/PBO 不达标),mock 如实反映。
 */
import type {
  StrategyState, GateVerdict, GateResult, BacktestResult, Execution,
  AuditDecision, ChainHealth, PaperAccount,
} from '@/types/api';

const mkIndicators = (names: [string, string][]): StrategyState['indicators'] =>
  names.map(([name, role], i) => ({
    name, role, enabled: i < 4,
    params: [
      { key: 'period', label: `${name.toLowerCase()}_period`, value: 10 + i * 2, min: 5, max: 50 },
      { key: 'mult', label: 'multiplier', value: 2 + i * 0.5, min: 1, max: 6, step: 0.5 },
    ],
  }));

export const MOCK_STRATEGIES: StrategyState[] = [
  {
    strategy_id: 'trend_dual', name: '策略1 趋势双向', mode: 'paper', regime: 'trend',
    position: 'BTC long 0.05', signals_today: 3,
    indicators: mkIndicators([['SuperTrend','primary_direction'],['EMA cross','direction_confirm'],['ADX','chop_filter'],['MACD','momentum_confirm'],['ATR','risk_sizing'],['Donchian','breakout']]),
    signal_logic: { entry: 'supertrend_dir AND ema_aligned AND adx > 25', exit: 'supertrend_flip OR atr_stop', min_confluence: 3, direction_mode: 'dual' },
    gate: { verdict: 'fail', dsr: 0.31, pbo: 0.62, reason: 'OOS fold 方差过高,DSR 低于阈值' },
  },
  {
    strategy_id: 'mean_revert', name: '策略2 均值回归', mode: 'shadow', regime: 'chop',
    position: '空仓', signals_today: 1,
    indicators: mkIndicators([['VWAP','primary_direction'],['Bollinger','reversion_band'],['RSI','overbought'],['Stochastic','timing'],['Keltner','volatility'],['ATR','risk_sizing']]),
    signal_logic: { entry: 'price < bb_lower AND rsi < 30', exit: 'price > vwap', min_confluence: 2, direction_mode: 'dual' },
    gate: { verdict: 'fail', dsr: 0.44, pbo: 0.58, reason: 'PBO 高于 0.5,过拟合风险' },
  },
  {
    strategy_id: 'momentum_multi', name: '策略3 多周期动量', mode: 'shadow', regime: 'bear',
    position: '空仓', signals_today: 0,
    indicators: mkIndicators([['EMA multi','trend_align'],['SuperTrend','primary_direction'],['ADX','strength'],['MACD','momentum_confirm'],['RSI','filter'],['OBV','volume_confirm']]),
    signal_logic: { entry: 'ema_stack_aligned AND adx > 30 AND obv_rising', exit: 'ema_cross_down', min_confluence: 4, direction_mode: 'long_only' },
    gate: { verdict: 'pending' },
  },
  {
    strategy_id: 'scalp_5m', name: '策略4 高频剥头皮', mode: 'shadow', regime: 'chop',
    position: '空仓', signals_today: 0,
    indicators: mkIndicators([['EMA fast','primary_direction'],['ATR','risk_sizing']]),
    signal_logic: { entry: 'ema_fast_cross', exit: 'atr_stop', min_confluence: 1, direction_mode: 'dual' },
    gate: { verdict: 'no-go' as GateVerdict, dsr: 0.18, pbo: 0.71, reason: '观察对象,DSR 过低暂不启用' },
  },
];

export const MOCK_GATE_RESULT: GateResult = {
  verdict: 'fail', gross_sr: 1.85, oos_sharpe: 0.42, dsr: 0.31, pbo: 0.62,
  reason: 'gross 信号真实但 OOS fold 方差高,DSR 0.31 < 阈值 0.95',
  global_trial_count: 47, dsr_threshold: 0.95,
};

export const MOCK_BACKTEST: BacktestResult = {
  equity_curve: Array.from({ length: 30 }, (_, i) => ({
    t: `D${i}`, gross: 1000 + i * 40 + Math.sin(i / 3) * 200, net: 1000 + i * 22 + Math.sin(i / 3) * 180,
  })),
  sharpe: 0.42, max_drawdown: -0.18, fill_count: 142, annualized: 0.11,
  folds: [
    { fold: 1, is_sharpe: 1.8, oos_sharpe: 0.9 },
    { fold: 2, is_sharpe: 1.6, oos_sharpe: -0.3 },
    { fold: 3, is_sharpe: 1.7, oos_sharpe: 0.6 },
    { fold: 4, is_sharpe: 1.5, oos_sharpe: -0.5 },
    { fold: 5, is_sharpe: 1.9, oos_sharpe: 0.2 },
  ],
  regime_breakdown: [
    { regime: 'trend', sharpe: 1.2 },
    { regime: 'chop', sharpe: -0.4 },
    { regime: 'bear', sharpe: -0.8 },
  ],
};

export const MOCK_EXECUTIONS: Execution[] = [
  { fill_id: 'f-1', time: '09:32', strategy: 'trend_dual', instrument: 'BTC-USDT', side: 'buy', qty: 0.05, backtest_price: 67400, actual_price: 67449 },
  { fill_id: 'f-2', time: '10:15', strategy: 'trend_dual', instrument: 'BTC-USDT', side: 'sell', qty: 0.05, backtest_price: 67800, actual_price: 67751 },
  { fill_id: 'f-3', time: '11:48', strategy: 'mean_revert', instrument: 'ETH-USDT', side: 'buy', qty: 0.5, backtest_price: 3520, actual_price: 3524 },
];

export const MOCK_DECISIONS: AuditDecision[] = [
  { event_id: 'evt-001', time: '09:32:01', strategy: 'trend_dual', event_type: 'signal_generated', conformance_tier: 'GOLD', signature_valid: true,
    decision_payload: { signal: 'long', strength: 0.72, indicators_agreed: ['supertrend', 'ema', 'adx'], regime: 'trend' } },
  { event_id: 'evt-002', time: '09:32:03', strategy: 'trend_dual', event_type: 'order_placed', conformance_tier: 'GOLD', signature_valid: true,
    decision_payload: { instrument: 'BTC-USDT', side: 'buy', qty: 0.05 } },
  { event_id: 'evt-003', time: '10:15:22', strategy: 'mean_revert', event_type: 'signal_generated', conformance_tier: 'SILVER', signature_valid: true,
    decision_payload: { signal: 'short', strength: 0.55 } },
];

export const MOCK_CHAIN: ChainHealth = { intact: true, gold_signed: true, latest_anchor: 'anchor-2026-06-15-0830' };

export const MOCK_ACCOUNT: PaperAccount = { balance: 10000, positions: 1, pnl_today_gross: 124.5, pnl_today_net: 87.3 };

// ── V2 mock(诚实空状态:paper fills=0,只有信号有数据)──────
import type {
  Position, Trade, SignalLog, StrategyStats, StrategyExecution, StrategyEquity,
  PortfolioSummary, CorrelationMatrix,
} from '@/types/api';

// 持仓/交易/执行:空(等首笔 fill)
export const MOCK_POSITIONS: Position[] = [];
export const MOCK_TRADES: Trade[] = [];
export const MOCK_EXECUTION: StrategyExecution = {
  fills: [], avg_slippage_bps: 0, max_slippage_bps: 0, backtest_assumed_bps: 2,
};

// 资金曲线:净值=初始(无变化)
export const MOCK_EQUITY: StrategyEquity = {
  points: [{ date: 'D0', equity: 5000, drawdown: 0, realized_pnl: 0 }],
};

// 统计:样本不足
export const MOCK_STATS: StrategyStats = {
  total_trades: 0, win_rate: 0, profit_factor: 0, avg_holding: '—',
  max_drawdown: 0, forward_sharpe: 0, total_pnl: 0,
  backtest_oos_sharpe: 0.42, sample_sufficient: false,
};

// 信号历史:现在有真数据(§1.4,8 条)
export const MOCK_SIGNALS: SignalLog[] = [
  { time: '2026-06-17 12:00', direction: 'neutral', strength: 0.2, acted: false,
    indicator_values: [
      { name: 'SuperTrend', value: 'flat' }, { name: 'ADX', value: 18.3 },
      { name: 'EMA cross', value: 'none' }, { name: 'MACD', value: -0.4 },
    ] },
  { time: '2026-06-17 08:00', direction: 'long', strength: 0.68, acted: false,
    indicator_values: [
      { name: 'SuperTrend', value: 'up' }, { name: 'ADX', value: 22.1 },
      { name: 'EMA cross', value: 'golden' }, { name: 'MACD', value: 0.6 },
    ] },
  { time: '2026-06-17 04:00', direction: 'neutral', strength: 0.31, acted: false,
    indicator_values: [
      { name: 'SuperTrend', value: 'up' }, { name: 'ADX', value: 19.8 },
      { name: 'EMA cross', value: 'none' }, { name: 'MACD', value: 0.1 },
    ] },
  { time: '2026-06-17 00:00', direction: 'short', strength: 0.55, acted: false,
    indicator_values: [
      { name: 'SuperTrend', value: 'down' }, { name: 'ADX', value: 26.4 },
      { name: 'RSI', value: 71.2 },
    ] },
  { time: '2026-06-16 20:00', direction: 'neutral', strength: 0.15, acted: false,
    indicator_values: [{ name: 'ADX', value: 14.2 }, { name: 'SuperTrend', value: 'flat' }] },
];

export const MOCK_PORTFOLIO_SUMMARY: PortfolioSummary = {
  total_positions: 0, total_unrealized_pnl: 0, total_realized_pnl: 0,
  net_exposure: [], margin_used: 0, available: 15000,
};

export const MOCK_CORRELATION: CorrelationMatrix = {
  strategies: ['donchian_4h', 'vwap_mr_1h', 'spot_trend_1d'],
  matrix: [[1, 0.12, 0.34], [0.12, 1, -0.08], [0.34, -0.08, 1]],
};
