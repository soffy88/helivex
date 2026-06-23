/**
 * helivex 后端 API 契约(snake_case,沿用 HelivexAuditDecision 等命名)
 * 来源:HELIVEX_FRONTEND_REQUIREMENTS.md §7。
 */

export type StrategyMode = 'shadow' | 'paper' | 'live';
export type Regime = 'trend' | 'chop' | 'bear' | 'bull' | 'unknown';
export type GateVerdict = 'pass' | 'fail' | 'pending' | 'no-go' | 'unknown';

export interface IndicatorConfig {
  name: string;
  enabled: boolean;
  role: string;
  params: { key: string; label: string; value: number; min: number; max: number; step?: number }[];
}

export interface StrategyState {
  strategy_id: string;
  name: string;
  mode: StrategyMode;
  regime: Regime;
  position: string;
  signals_today: number;
  indicators: IndicatorConfig[];
  signal_logic: { entry: string; exit: string; min_confluence: number; direction_mode: 'dual' | 'long_only' | 'short_only' };
  gate: { verdict: GateVerdict; dsr?: number; pbo?: number; reason?: string };
}

export interface GateResult {
  verdict: GateVerdict;
  gross_sr: number;
  oos_sharpe: number;
  dsr: number;
  pbo: number;
  reason?: string;
  global_trial_count: number;
  dsr_threshold: number;
}

export interface BacktestResult {
  equity_curve: { t: string; gross: number; net: number }[];
  sharpe: number;
  max_drawdown: number;
  fill_count: number;
  annualized: number;
  folds: { fold: number; is_sharpe: number; oos_sharpe: number }[];
  regime_breakdown: { regime: Regime; sharpe: number }[];
}

export interface Execution {
  fill_id: string;
  time: string;
  strategy: string;
  instrument: string;
  side: 'buy' | 'sell';
  qty: number;
  backtest_price: number;
  actual_price: number;
}

// Real /executions response (gateway). No mock — honest empty when no fills.
export interface Fill {
  id: number;
  ts: string;
  strategy_id: string;
  instrument: string;
  side: string;
  quantity: number;
  signal_price: number | null;
  actual_fill_price: number;
  slippage_bps: number | null;
  order_id: string;
  latency_ms: number | null;
  fill_type: string;
}

export interface FidelityRow {
  strategy_id: string;
  n_signals: number;
  n_fills: number;
  fill_rate: number | null;
  mean_slippage_bps: number | null;
  p95_slippage_bps: number | null;
  mean_latency_ms: number | null;
}

export interface ExecutionsResponse {
  fidelity: FidelityRow[];
  fills: Fill[];
}

export interface AuditDecision {
  event_id: string;
  time: string;
  strategy: string;
  event_type: string;
  conformance_tier: 'GOLD' | 'SILVER';
  signature_valid: boolean;
  decision_payload?: Record<string, unknown>;
}

export interface ChainHealth {
  intact: boolean;
  gold_signed: boolean;
  latest_anchor: string;
  break_point?: string;
}

export interface PaperAccount {
  balance: number;
  positions: number;
  pnl_today_gross: number;
  pnl_today_net: number;
}

// ── V2 策略详情页类型 ──────────────────────────────

export interface Position {
  instrument: string;
  side: 'long' | 'short';
  quantity: number;
  avg_entry_price: number;
  current_price: number;
  unrealized_pnl: number;
  unrealized_pnl_pct: number;
  holding_duration: string;
  margin_used?: number;
  leverage?: number;
  liquidation_price?: number;
}

export interface Trade {
  trade_id: string;
  open_time: string;
  close_time: string;
  instrument: string;
  side: 'long' | 'short';
  entry_price: number;
  exit_price: number;
  quantity: number;
  realized_pnl: number;
  fees: number;
  funding_cost?: number;
  holding_duration: string;
  trigger_signal: string;
  exit_reason: string;
}

export interface EquitySeriesPoint {
  date: string;
  equity: number;
  drawdown?: number;        // 0..-1
  realized_pnl?: number;
}

export interface StrategyEquity {
  points: EquitySeriesPoint[];
  by_instrument?: { instrument: string; pnl: number }[];
}

export interface IndicatorSnapshot {
  name: string;
  value: number | string;
}

export interface SignalLog {
  time: string;
  direction: 'long' | 'short' | 'neutral';
  strength: number;
  indicator_values: IndicatorSnapshot[];
  acted: boolean;
}

export interface StrategyStats {
  total_trades: number;
  win_rate: number;
  profit_factor: number;
  avg_holding: string;
  max_drawdown: number;
  forward_sharpe: number;
  total_pnl: number;
  backtest_oos_sharpe?: number;   // vs backtest 对比
  sample_sufficient: boolean;      // n>=30
}

export interface StrategyExecution {
  fills: {
    fill_id: string;
    time: string;
    instrument: string;
    expected_price: number;
    actual_price: number;
    liquidity: 'maker' | 'taker';
  }[];
  avg_slippage_bps: number;
  max_slippage_bps: number;
  backtest_assumed_bps: number;
}

// ── Portfolio 全局组合 ──────────────────────────────
export interface PortfolioEquity {
  combined: EquitySeriesPoint[];
  by_strategy: { strategy_id: string; points: EquitySeriesPoint[]; contribution_pct: number }[];
}

export interface CorrelationMatrix {
  strategies: string[];
  matrix: number[][];   // [i][j] 相关系数
}

export interface PortfolioSummary {
  total_positions: number;
  total_unrealized_pnl: number;
  total_realized_pnl: number;
  net_exposure: { instrument: string; net: number }[];
  margin_used: number;
  available: number;
}
