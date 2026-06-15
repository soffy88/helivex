/**
 * helivex 后端 API 契约(snake_case,沿用 HelivexAuditDecision 等命名)
 * 来源:HELIVEX_FRONTEND_REQUIREMENTS.md §7。
 */

export type StrategyMode = 'shadow' | 'paper' | 'live';
export type Regime = 'trend' | 'chop' | 'bear' | 'bull' | 'unknown';
export type GateVerdict = 'pass' | 'fail' | 'pending';

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
