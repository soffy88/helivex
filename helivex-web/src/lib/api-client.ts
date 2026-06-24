/**
 * helivex API client — 对接 api-gateway(:8765)。全部真实数据,无 mock。
 * 文档 §7 endpoint。
 */
import type {
  StrategyState, BacktestResult, ExecutionsResponse,
  AuditDecision, ChainHealth, PaperAccount,
} from '@/types/api';

export const API_BASE = process.env.NEXT_PUBLIC_API_BASE ?? 'http://localhost:8765';

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
  getConfig:     (id: string) => req<Record<string, unknown>>(`/strategies/${id}/config`),
  putConfig:     (id: string, config: Record<string, unknown>) => req<{ ok: boolean; path: string }>(`/strategies/${id}/config`, { method: 'PUT', body: JSON.stringify(config) }),
  gateTrials:    () => req<unknown>('/gate/trials'),
  runBacktest:   (body: unknown) => req<BacktestResult>('/backtest/run', { method: 'POST', body: JSON.stringify(body) }),
  executions:    () => req<ExecutionsResponse>('/executions'),
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
