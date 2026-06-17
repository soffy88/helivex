/**
 * helivex API client — 对接重建后 api-gateway。
 * 文档 §7 endpoint。USE_MOCK=true 时用 mock。
 */
import type {
  StrategyState, GateResult, BacktestResult, Execution,
  AuditDecision, ChainHealth, PaperAccount, IndicatorConfig,
} from '@/types/api';

export const API_BASE = process.env.NEXT_PUBLIC_API_BASE ?? 'http://localhost:8080';
export const USE_MOCK = (process.env.NEXT_PUBLIC_USE_MOCK ?? 'true').toLowerCase() === 'true';

async function req<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, {
    ...init,
    headers: { 'Content-Type': 'application/json', ...(init?.headers as Record<string, string>) },
  });
  if (!res.ok) throw new Error(`${res.status}`);
  return res.json() as Promise<T>;
}

export const helivexApi = {
  strategies:    () => req<StrategyState[]>('/strategies'),
  getConfig:     (id: string) => req<IndicatorConfig[]>(`/strategies/${id}/config`),
  putConfig:     (id: string, config: IndicatorConfig[]) => req<void>(`/strategies/${id}/config`, { method: 'PUT', body: JSON.stringify(config) }),
  runGate:       (id: string, config?: IndicatorConfig[]) => req<GateResult>('/gate/run', { method: 'POST', body: JSON.stringify({ strategy_id: id, config }) }),
  gateTrials:    () => req<GateResult[]>('/gate/trials'),
  runBacktest:   (body: unknown) => req<BacktestResult>('/backtest/run', { method: 'POST', body: JSON.stringify(body) }),
  executions:    () => req<Execution[]>('/executions'),
  decisions:     () => req<AuditDecision[]>('/audit/decisions'),
  verifySig:     (eventId: string) => req<{ valid: boolean }>(`/audit/verify_signature?event_id=${eventId}`),
  chainHealth:   () => req<ChainHealth>('/audit/chain/verify'),
  account:       () => req<PaperAccount>('/paper/account'),
  setMode:       (id: string, mode: string) => req<void>(`/strategies/${id}/mode`, { method: 'PUT', body: JSON.stringify({ mode }) }),
};

// ── V2 策略详情 + Portfolio endpoint(§4)──────────
import type {
  Position, Trade, StrategyEquity, SignalLog, StrategyStats, StrategyExecution,
  PortfolioEquity, CorrelationMatrix, PortfolioSummary,
} from '@/types/api';

export const detailApi = {
  positions: (id: string) => req<Position[]>(`/strategies/${id}/positions`),
  trades:    (id: string) => req<Trade[]>(`/strategies/${id}/trades`),
  equity:    (id: string) => req<StrategyEquity>(`/strategies/${id}/equity`),
  signals:   (id: string) => req<SignalLog[]>(`/strategies/${id}/signals`),
  stats:     (id: string) => req<StrategyStats>(`/strategies/${id}/stats`),
  execution: (id: string) => req<StrategyExecution>(`/strategies/${id}/execution`),
};

export const portfolioApi = {
  equity:      () => req<PortfolioEquity>('/portfolio/equity'),
  correlation: () => req<CorrelationMatrix>('/portfolio/correlation'),
  summary:     () => req<PortfolioSummary>('/portfolio/summary'),
  kill:        () => req<void>('/portfolio/kill', { method: 'POST' }),
};
