/**
 * helivex API client — adapts api-gateway (:8765) responses to frontend types.
 * USE_MOCK=false: calls real gateway; USE_MOCK=true: callers use mock-data directly.
 */
import type {
  StrategyState, GateResult, BacktestResult, Execution,
  AuditDecision, ChainHealth, PaperAccount,
} from '@/types/api';

export const API_BASE = process.env.NEXT_PUBLIC_API_BASE ?? 'http://localhost:8765';
export const USE_MOCK = (process.env.NEXT_PUBLIC_USE_MOCK ?? 'true').toLowerCase() === 'true';

async function req<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, {
    ...init,
    headers: { 'Content-Type': 'application/json', ...(init?.headers as Record<string, string>) },
  });
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  return res.json() as Promise<T>;
}

// ── Raw gateway shapes ─────────────────────────────────────────────────────

interface GwStrategy {
  id: string; strategy: string; timeframe: string;
  instruments: string[]; mode: string;
  last_gate_verdict: string | null; gate_passed: boolean;
  n_paper_signals: number; config_path: string;
}

interface GwAuditDecision {
  id: number; ts: string; strategy_id: string; instrument: string;
  action: string; signal_price: number;
  audit_record_id: string; fingerprint_hex: string;
  has_signature: boolean; tier: 'GOLD' | 'STANDARD';
}

interface GwChainVerify {
  ok: boolean; n_total: number; n_gold: number; n_valid: number;
  records: { id: number; valid: boolean | null; tier: string }[];
}

interface GwAnchors {
  first: { id: number; ts: string; fingerprint_hex: string; sig_b64: string } | null;
  last:  { id: number; ts: string; fingerprint_hex: string; sig_b64: string } | null;
}

interface GwFill {
  id: number; ts: string; strategy_id: string; instrument: string;
  side: string; quantity: number; signal_price: number | null;
  actual_fill_price: number; slippage_bps: number | null;
  order_id: string; latency_ms: number | null; fill_type: string;
}

interface GwExecutions {
  fidelity: {
    strategy_id: string; n_signals: number; n_fills: number;
    fill_rate: number | null; mean_slippage_bps: number | null;
    p95_slippage_bps: number | null; mean_latency_ms: number | null;
  }[];
  fills: GwFill[];
}

interface GwGateRun {
  overall_status: string; trial_n: number;
  instruments: Record<string, {
    oos_sharpes: number[]; is_sharpes: number[];
    mean_oos_sharpe: number; pbo: number;
    deflated_sharpe: number; fail_reasons: string[];
    status: string; gross_sharpe: number;
    adjusted_dsr: number; dsr_threshold: number; trial_n: number;
  }>;
}

interface GwTrials {
  total_trials: number;
  history: {
    trial_n: number; config: string; verdict: string;
    metrics: {
      instruments: Record<string, { status: string; dsr: number; pbo: number; mean_oos: number; gross_sharpe: number }>;
      overall: string;
    };
  }[];
}

interface GwBacktest {
  overall_status?: string; verdict?: string;
  instrument: string; n_bars: number; n_signals: number;
  pnl: number[]; regime: string[];
  mean_oos_sharpe?: number; pbo?: number; deflated_sharpe?: number;
  instruments?: Record<string, { mean_oos_sharpe?: number; pbo?: number; deflated_sharpe?: number; gross_sharpe?: number; oos_sharpes?: number[] }>;
}

// ── Adapters ───────────────────────────────────────────────────────────────

/** Merge gateway summary into a StrategyState using mock as structural base */
export function mergeGatewayStrategy(mock: StrategyState, gw: GwStrategy): StrategyState {
  return {
    ...mock,
    strategy_id: gw.id,
    mode: (gw.mode || mock.mode) as StrategyState['mode'],
    signals_today: gw.n_paper_signals,
    gate: {
      ...mock.gate,
      verdict: gw.last_gate_verdict
        ? (gw.last_gate_verdict.toLowerCase() as StrategyState['gate']['verdict'])
        : 'pending',
    },
  };
}

function adaptGateRun(raw: GwGateRun): GateResult {
  const instVals = Object.values(raw.instruments ?? {});
  const avg = (fn: (v: typeof instVals[0]) => number) =>
    instVals.length ? instVals.reduce((s, v) => s + fn(v), 0) / instVals.length : 0;
  const reasons = instVals.flatMap(v => v.fail_reasons ?? []);
  const firstInst = instVals[0];
  return {
    verdict: (raw.overall_status ?? 'fail').toLowerCase() as GateResult['verdict'],
    gross_sr: avg(v => v.gross_sharpe ?? 0),
    oos_sharpe: avg(v => v.mean_oos_sharpe ?? 0),
    dsr: avg(v => v.deflated_sharpe ?? 0),
    pbo: avg(v => v.pbo ?? 0),
    reason: reasons.length ? reasons.join('; ') : undefined,
    global_trial_count: raw.trial_n ?? 0,
    dsr_threshold: firstInst?.dsr_threshold ?? 0.95,
  };
}

function adaptTrialToGateResult(t: GwTrials['history'][0], total_trials: number): GateResult {
  const instVals = Object.values(t.metrics?.instruments ?? {});
  const avg = (fn: (v: typeof instVals[0]) => number) =>
    instVals.length ? instVals.reduce((s, v) => s + fn(v), 0) / instVals.length : 0;
  return {
    verdict: (t.verdict ?? 'fail').toLowerCase() as GateResult['verdict'],
    gross_sr: avg(v => v.gross_sharpe ?? 0),
    oos_sharpe: avg(v => v.mean_oos ?? 0),
    dsr: avg(v => v.dsr ?? 0),
    pbo: avg(v => v.pbo ?? 0),
    global_trial_count: total_trials,
    dsr_threshold: 0.95,
  };
}

function adaptAuditDecision(d: GwAuditDecision): AuditDecision {
  return {
    event_id: String(d.id),
    time: d.ts ? d.ts.slice(11, 19) : '',
    strategy: d.strategy_id,
    event_type: d.action || 'signal',
    conformance_tier: d.tier === 'GOLD' ? 'GOLD' : 'SILVER',
    signature_valid: d.has_signature,
    decision_payload: {
      instrument: d.instrument,
      action: d.action,
      signal_price: d.signal_price,
      audit_record_id: d.audit_record_id,
      fingerprint_hex: d.fingerprint_hex,
    },
  };
}

function adaptChainHealth(chain: GwChainVerify, anchors: GwAnchors): ChainHealth {
  return {
    intact: chain.ok,
    gold_signed: chain.n_gold > 0,
    latest_anchor: anchors.last?.ts?.slice(0, 19).replace('T', ' ') ?? '—',
    break_point: chain.ok ? undefined : `record broken`,
  };
}

function adaptFill(f: GwFill): Execution {
  return {
    fill_id: String(f.id),
    time: f.ts ? f.ts.slice(11, 19) : '',
    strategy: f.strategy_id,
    instrument: f.instrument,
    side: f.side.toLowerCase() as 'buy' | 'sell',
    qty: f.quantity,
    backtest_price: f.signal_price ?? f.actual_fill_price,
    actual_price: f.actual_fill_price,
  };
}

function adaptBacktest(raw: GwBacktest): BacktestResult {
  // Build equity curve from cumulative pnl array
  const pnl = raw.pnl ?? [];
  let cum = 0;
  const equity_curve = pnl.map((p, i) => {
    cum += p;
    return { t: `B${i}`, gross: 1000 + cum * 1000, net: 1000 + cum * 950 };
  });

  // Regime breakdown from regime labels
  const regimeCounts: Record<string, { sum: number; n: number }> = {};
  raw.regime?.forEach((r, i) => {
    if (!regimeCounts[r]) regimeCounts[r] = { sum: 0, n: 0 };
    regimeCounts[r].sum += pnl[i] ?? 0;
    regimeCounts[r].n++;
  });
  const regime_breakdown = Object.entries(regimeCounts).map(([regime, { sum, n }]) => ({
    regime: regime as BacktestResult['regime_breakdown'][0]['regime'],
    sharpe: n > 1 ? parseFloat((sum / n / (Math.sqrt(n) || 1) * 10).toFixed(2)) : 0,
  }));

  // Try to get folds from instruments
  const instVals = Object.values(raw.instruments ?? {});
  const folds = instVals.length > 0 && instVals[0].oos_sharpes
    ? instVals[0].oos_sharpes.map((oos, i) => ({
        fold: i + 1,
        is_sharpe: (instVals[0] as { is_sharpes?: number[] }).is_sharpes?.[i] ?? 0,
        oos_sharpe: oos,
      }))
    : [];

  const instValues = Object.values(raw.instruments ?? {});
  const avgOos = instValues.length
    ? instValues.reduce((s, v) => s + (v.mean_oos_sharpe ?? 0), 0) / instValues.length
    : 0;
  const avgPbo = instValues.length
    ? instValues.reduce((s, v) => s + (v.pbo ?? 0), 0) / instValues.length
    : 0;

  // Simple metrics from pnl
  const returns = pnl;
  const mean = returns.length ? returns.reduce((a, b) => a + b, 0) / returns.length : 0;
  const std = returns.length > 1
    ? Math.sqrt(returns.reduce((s, r) => s + (r - mean) ** 2, 0) / returns.length)
    : 1;
  const sharpe = std > 0 ? parseFloat((mean / std * Math.sqrt(252)).toFixed(2)) : 0;

  let peak = 0, maxDD = 0, runCum = 0;
  for (const r of returns) {
    runCum += r;
    if (runCum > peak) peak = runCum;
    const dd = peak - runCum;
    if (dd > maxDD) maxDD = dd;
  }

  return {
    equity_curve: equity_curve.length ? equity_curve : [{ t: 'D0', gross: 1000, net: 1000 }],
    sharpe: isNaN(sharpe) ? avgOos : sharpe,
    max_drawdown: -(maxDD),
    fill_count: raw.n_signals ?? 0,
    annualized: parseFloat((mean * 252).toFixed(3)),
    folds: folds.length ? folds : [{ fold: 1, is_sharpe: 0, oos_sharpe: avgOos }],
    regime_breakdown: regime_breakdown.length ? regime_breakdown : [{ regime: 'unknown', sharpe: 0 }],
  };
}

// ── Public API ─────────────────────────────────────────────────────────────

export const helivexApi = {
  strategies: () => req<GwStrategy[]>('/strategies'),

  gateTrials: async (): Promise<GateResult[]> => {
    const d = await req<GwTrials>('/gate/trials');
    return (d.history ?? []).map(t => adaptTrialToGateResult(t, d.total_trials));
  },

  runGate: async (strategyId: string): Promise<GateResult> => {
    const d = await req<GwGateRun>(`/gate/run?config=${encodeURIComponent(strategyId)}`, { method: 'POST' });
    return adaptGateRun(d);
  },

  decisions: async (): Promise<AuditDecision[]> => {
    const d = await req<GwAuditDecision[]>('/audit/decisions?limit=50');
    return d.map(adaptAuditDecision);
  },

  chainHealth: async (): Promise<ChainHealth> => {
    const [chain, anchors] = await Promise.all([
      req<GwChainVerify>('/audit/chain/verify'),
      req<GwAnchors>('/anchors'),
    ]);
    return adaptChainHealth(chain, anchors);
  },

  verifySig: (eventId: string) => req<{ valid: boolean }>(`/audit/event/${eventId}`).then(r => ({ valid: !!(r as Record<string, unknown>).sig_b64 })),

  executions: async (): Promise<{ fills: Execution[]; fidelity: GwExecutions['fidelity'] }> => {
    const d = await req<GwExecutions>('/executions');
    return { fills: (d.fills ?? []).map(adaptFill), fidelity: d.fidelity ?? [] };
  },

  account: () => req<PaperAccount>('/paper/account'),

  runBacktest: async (strategyId: string, instrument: string): Promise<BacktestResult> => {
    const d = await req<GwBacktest>(`/backtest/run?config=${encodeURIComponent(strategyId)}&instrument=${encodeURIComponent(instrument)}`, { method: 'POST' });
    return adaptBacktest(d);
  },

  health: () => req<{ ok: boolean; ts: string }>('/health'),
};
