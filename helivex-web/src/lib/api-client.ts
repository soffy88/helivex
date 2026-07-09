/**
 * helivex API client — 对接 api-gateway(:8765)。全部真实数据,无 mock。
 * 文档 §7 endpoint。
 */
import type {
  StrategyState, BacktestResult, ExecutionsResponse,
  AuditDecision, ChainHealth, PaperAccount,
} from '@/types/api';

// Same-origin by default — the browser hits /gw/* on this server, which Next
// rewrites to the gateway (see next.config.ts). Override with NEXT_PUBLIC_API_BASE.
export const API_BASE = process.env.NEXT_PUBLIC_API_BASE ?? '/gw';

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
  restartPaper:  () => req<{ ok: boolean; message?: string; reason?: string }>('/paper/restart', { method: 'POST' }),
  gateTrials:    () => req<unknown>('/gate/trials'),
  gateRun:       (config: string, instrument?: string) =>
    req<{ overall_status?: string; trial_n?: number; instruments?: Record<string, unknown> }>(
      `/gate/run?config=${encodeURIComponent(config)}${instrument ? `&instrument=${encodeURIComponent(instrument)}` : ''}&quiet=true`,
      { method: 'POST' }),
  runBacktest:   (body: unknown) => req<BacktestResult>('/backtest/run', { method: 'POST', body: JSON.stringify(body) }),
  executions:    () => req<ExecutionsResponse>('/executions'),
  decisions:     () => req<AuditDecision[]>('/audit/decisions'),
  verifySig:     (rec: { fingerprint_hex: string; sig_b64: string; public_key_b64?: string }) =>
    req<{ valid: boolean }>('/verify_signature', { method: 'POST', body: JSON.stringify(rec) }),
  chainHealth:   () => req<ChainHealth>('/audit/chain/verify'),
  account:       () => req<PaperAccount>('/paper/account'),
  setMode:       (id: string, mode: string, force = false) =>
    req<void>(`/strategies/${id}/mode?mode=${encodeURIComponent(mode)}${force ? '&force=true' : ''}`, { method: 'PUT' }),
};

// ── V2 策略详情 + Portfolio endpoint(§4)──────────
import type {
  Position, Trade, StrategyEquity, SignalLog, StrategyStats, StrategyExecution,
  PortfolioEquity, CorrelationMatrix, PortfolioSummary, CvarWeights, PositionCaps,
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
  cvarWeights: () => req<CvarWeights>('/portfolio/cvar_weights'),
  positionCaps: () => req<PositionCaps>('/portfolio/position_caps'),
};

// ── R14 risk layer + R16 L2 microstructure ──────────────
import type { RiskStatus, RiskEvent, MicroLatest } from '@/types/api';

export const riskApi = {
  status: () => req<RiskStatus>('/risk/status'),
  events: () => req<RiskEvent[]>('/risk/events?limit=30'),
  kill:   (reason?: string) => req<{ ok: boolean; tripped: boolean }>('/risk/kill', { method: 'POST', body: JSON.stringify({ reason }) }),
  reset:  () => req<{ ok: boolean; tripped: boolean }>('/risk/reset', { method: 'POST' }),
};

export const microApi = {
  latest: () => req<MicroLatest>('/microstructure/latest?series=60'),
};

// ── LER(HELIVEX-IMPL_SPEC-LER-001)数据积累进度 + 策略参数 ───────────
import type { LerCoverage, LerConfig } from '@/types/api';

export const lerApi = {
  coverage: () => req<LerCoverage>('/research/ler/coverage'),
  config: () => req<LerConfig>('/research/ler/config'),
};

// ── 3O 共识大脑(P2-P6)──────────
import type { RegimeResp, EnginesResp, ConsensusResp, ConsensusRiskResp } from '@/types/api';

import type { EngineWeightsResp, ConsensusConfigResp } from '@/types/api';

export const ensembleApi = {
  regime:   () => req<RegimeResp>('/regime'),
  engines:  () => req<EnginesResp>('/engines'),
  consensus: () => req<ConsensusResp>('/consensus'),
  riskEval: () => req<ConsensusRiskResp>('/consensus/risk_eval'),
  // 补齐 G: 共识层在线调参(observe-only,只调判据不下单)
  weights:  () => req<EngineWeightsResp>('/engines/weights'),
  consensusConfig: () => req<ConsensusConfigResp>('/consensus/config'),
  putConsensusConfig: (base_threshold: number) =>
    req<{ ok: boolean; base_threshold: number }>('/consensus/config', { method: 'PUT', body: JSON.stringify({ base_threshold }) }),
  putEngineWeights: (weights: { engine: string; base_weight: number }[]) =>
    req<{ ok: boolean; applied: { engine: string; base_weight: number }[] }>('/engines/weights', { method: 'PUT', body: JSON.stringify({ weights }) }),
};

// ── 补齐 C: K线 + 决策轨迹 ──────────
import type { OhlcvResp, DecisionTrailResp } from '@/types/api';

export const chartApi = {
  ohlcv: (symbol: string, limit = 120) => req<OhlcvResp>(`/ohlcv/${encodeURIComponent(symbol)}?limit=${limit}`),
  decisionTrail: (limit = 20) => req<DecisionTrailResp>(`/decision-trail/recent?limit=${limit}`),
};
