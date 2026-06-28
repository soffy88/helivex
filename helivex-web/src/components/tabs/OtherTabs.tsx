/**
 * 其余 Tab:Backtest / Executions / P&L / Audit
 * 全部真实数据(无 mock)。无数据 → 诚实空状态。
 * (Strategies tab 已废弃 — 平铺主页 OverviewTab 的策略选择器取代了它)
 */
'use client';

import { useState } from 'react';
import { OEquityCurveChart } from '@helios/blocks';
import { SafeGateBadge } from '../SafeBadges';
import { EmptyState, Skeleton, StaleBanner } from '../EmptyState';
import { DivergingBars } from '../charts';
import { helivexApi, portfolioApi } from '@/lib/api-client';
import { useApi } from '@/lib/use-api';
import type { ExecutionsResponse, PortfolioEquity } from '@/types/api';

const fmt = (v: number | null | undefined, d = 1, suffix = '') =>
  v === null || v === undefined ? '—' : `${v.toFixed(d)}${suffix}`;

// ── Backtest Tab (真实 gate 账本 /gate/trials) ──────────────────────
interface TrialInst { status: string; dsr: number; pbo: number; mean_oos: number; gross_sharpe: number; }
interface Trial { trial_n: number; config: string; verdict: string; metrics: { instruments: Record<string, TrialInst>; overall: string }; }
interface GateLedger { total_trials: number; history: Trial[]; }

export function BacktestTab() {
  const { data, loading, error, stale } = useApi(() => helivexApi.gateTrials() as unknown as Promise<GateLedger>, [], undefined, 'gate');
  if (loading && !data) return <div className="hv-tab"><Skeleton /></div>;
  if (error && !data) return <div className="hv-tab"><EmptyState text="网关连接失败" sub={error} /></div>;
  const hist = data?.history ?? [];
  const passes = hist.filter(t => t.verdict?.toUpperCase() === 'PASS').length;
  return (
    <div className="hv-tab">
      {stale && <StaleBanner error={error!} />}
      <div className="hv-section-title">Gate 账本(真实,全局 N = {data?.total_trials ?? 0})</div>
      {hist.length === 0 ? <EmptyState text="暂无 gate 记录" /> : (
        <>
          <div className="hv-honest-note">{passes}/{hist.length} 个配置过 gate。所有 DSR/PBO 经 walk-forward + 全局 N 多重检验校正。</div>
          <div className="hv-section-title" style={{ marginTop: 4 }}>各 trial 平均 DSR(去通胀夏普,&gt;0 才可能过)</div>
          <DivergingBars
            items={hist.slice().reverse().map(t => {
              const insts = Object.values(t.metrics?.instruments ?? {});
              const dsrs = insts.map(m => m.dsr).filter((d): d is number => typeof d === 'number');
              const mean = dsrs.length ? dsrs.reduce((a, b) => a + b, 0) / dsrs.length : null;
              return { label: `#${t.trial_n} ${t.config?.split('/').pop()?.replace(/\.(yaml|py).*/, '').slice(0, 16) ?? ''}`,
                       value: mean, ok: t.verdict?.toUpperCase() === 'PASS' };
            })}
          />
          <table className="hv-table">
            <thead><tr><th>#</th><th>配置</th><th>裁决</th><th>每标的 DSR / PBO</th></tr></thead>
            <tbody>
              {hist.slice().reverse().map(t => (
                <tr key={t.trial_n}>
                  <td className="hv-num">{t.trial_n}</td>
                  <td><code>{t.config?.split('/').pop()}</code></td>
                  <td><SafeGateBadge verdict={t.verdict} compact /></td>
                  <td className="hv-num" style={{ fontSize: 'var(--text-sm)' }}>
                    {Object.entries(t.metrics?.instruments ?? {}).map(([inst, m]) =>
                      `${inst.split('-')[0]}: ${fmt(m.dsr, 2)}/${fmt(m.pbo, 2)}`).join('  ·  ') || '—'}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      )}
    </div>
  );
}

// ── Executions Tab (真实 /executions,无 mock,无成交即诚实空状态)──────────
export function ExecutionsTab() {
  const { data, loading, error, stale } = useApi<ExecutionsResponse>(() => helivexApi.executions(), [], 15000, 'executions');
  if (loading && !data) return <div className="hv-tab"><Skeleton /></div>;
  if (error && !data) return <div className="hv-tab"><EmptyState text="网关连接失败" sub={`/executions: ${error}`} /></div>;
  const fidelity = data?.fidelity ?? [];
  const fills = data?.fills ?? [];
  const sideColor = (s: string) => s.toUpperCase() === 'BUY' ? 'var(--success,#3fb950)' : 'var(--destructive)';
  return (
    <div className="hv-tab">
      {stale && <StaleBanner error={error!} />}
      <div className="hv-section-title">执行真实度(真实成交 vs backtest 假设)</div>
      {fidelity.length === 0 ? (
        <EmptyState text="尚无真实成交 — 无执行真实度可算" sub="backtest 假设需首笔真实 fill 才能校验。绝不用假数据冒充。" />
      ) : (
        <table className="hv-table">
          <thead><tr><th>策略</th><th>signals</th><th>fills</th><th>fill rate</th><th>平均滑点</th><th>p95 滑点</th><th>平均延迟</th></tr></thead>
          <tbody>
            {fidelity.map(f => (
              <tr key={f.strategy_id}>
                <td>{f.strategy_id}</td>
                <td className="hv-num">{f.n_signals}</td>
                <td className="hv-num">{f.n_fills}</td>
                <td className="hv-num">{f.fill_rate === null ? '—' : `${(f.fill_rate * 100).toFixed(1)}%`}</td>
                <td className="hv-num">{fmt(f.mean_slippage_bps, 1, ' bps')}</td>
                <td className="hv-num">{fmt(f.p95_slippage_bps, 1, ' bps')}</td>
                <td className="hv-num">{fmt(f.mean_latency_ms, 0, ' ms')}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      <div className="hv-section-title">Fill 列表(真实成交)</div>
      {fills.length === 0 ? (
        <EmptyState text="尚无真实 fill" sub="四策略下单后等市场成交;首笔 fill 出现即记录真实滑点 / maker-taker / 延迟。" />
      ) : (
        <table className="hv-table">
          <thead><tr><th>时间</th><th>策略</th><th>品种</th><th>方向</th><th>数量</th><th>信号价</th><th>真实价</th><th>滑点</th><th>类型</th><th>延迟</th></tr></thead>
          <tbody>
            {fills.map(f => (
              <tr key={f.id}>
                <td>{new Date(f.ts).toLocaleString()}</td>
                <td>{f.strategy_id}</td>
                <td>{f.instrument}</td>
                <td style={{ color: sideColor(f.side) }}>{f.side}</td>
                <td className="hv-num">{f.quantity}</td>
                <td className="hv-num">{f.signal_price ?? '—'}</td>
                <td className="hv-num">{f.actual_fill_price}</td>
                <td className="hv-num">{fmt(f.slippage_bps, 1, ' bps')}</td>
                <td>{f.fill_type}</td>
                <td className="hv-num">{f.latency_ms === null ? '—' : `${f.latency_ms} ms`}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}

// ── P&L Tab (真实合并资金曲线 /portfolio/equity) ────────────────────
export function PnLTab() {
  const { data, loading, error, stale } = useApi<PortfolioEquity>(() => portfolioApi.equity(), [], 30000, 'pnl');
  if (loading && !data) return <div className="hv-tab"><Skeleton /></div>;
  if (error && !data) return <div className="hv-tab"><EmptyState text="网关连接失败" sub={error} /></div>;
  const pts = data?.combined ?? [];
  return (
    <div className="hv-tab">
      {stale && <StaleBanner error={error!} />}
      <div className="hv-section-title">累计 P&L(真实成交派生)</div>
      {pts.length < 2 ? <EmptyState text="数据不足" sub="需 ≥2 个成交点才能画曲线" /> : (
        <div className="hv-chart-box">
          <OEquityCurveChart points={pts.map(p => ({ date: p.date, equity: p.equity, drawdown: p.drawdown }))} showDrawdown />
        </div>
      )}
      <div className="hv-honest-note">⚠️ Paper 短期 P&L ≠ 策略有效。当前 gate 全 FAIL/NO-GO,此曲线含运气成分,不代表可上 live。</div>
    </div>
  );
}

// ── Audit Tab (真实 GOLD 链 /audit/decisions) ───────────────────────
interface Decision {
  id: number; ts: string; strategy_id: string; instrument: string; action: string;
  signal_price: number; audit_record_id: string; fingerprint_hex: string;
  has_signature: boolean; tier: string;
}
export function AuditTab() {
  const { data, loading, error, stale } = useApi<Decision[]>(() => helivexApi.decisions() as unknown as Promise<Decision[]>, [], 15000, 'audit');
  const [sel, setSel] = useState<number | null>(null);
  if (loading && !data) return <div className="hv-tab"><Skeleton /></div>;
  if (error && !data) return <div className="hv-tab"><EmptyState text="网关连接失败" sub={error} /></div>;
  const decisions = data ?? [];
  if (decisions.length === 0) return <div className="hv-tab"><EmptyState text="暂无审计记录" /></div>;
  const selected = decisions.find(d => d.id === sel) ?? decisions[0]!;
  return (
    <div className="hv-tab">
      {stale && <StaleBanner error={error!} />}
      <div className="hv-section-title">决策链(GOLD Ed25519 签名,真实)</div>
      <div className="hv-audit-layout">
        <div className="hv-audit-list">
          {decisions.map(d => (
            <button key={d.id} className="hv-audit-item"
              data-active={selected.id === d.id ? 'true' : undefined}
              onClick={() => setSel(d.id)}>
              <span className="hv-audit-time">{new Date(d.ts).toLocaleString()}</span>
              <span className="hv-audit-type">{d.action}</span>
              <span className="hv-tier-badge" data-tier={d.tier}>{d.tier}</span>
              <span className="hv-sig" style={{ color: d.has_signature ? 'var(--success,#3fb950)' : 'var(--destructive)' }}>
                {d.has_signature ? '✓' : '✕'}
              </span>
            </button>
          ))}
        </div>
        <div className="hv-audit-detail">
          <div className="hv-audit-detail__head">
            <span>{selected.strategy_id}</span>
            <span className="hv-tier-badge" data-tier={selected.tier}>{selected.tier} {selected.has_signature ? '✓ 已签名' : '✕ 无签名'}</span>
          </div>
          <pre className="hv-json">{JSON.stringify({
            id: selected.id, ts: selected.ts, instrument: selected.instrument,
            action: selected.action, signal_price: selected.signal_price,
            audit_record_id: selected.audit_record_id, fingerprint: selected.fingerprint_hex,
          }, null, 2)}</pre>
        </div>
      </div>
    </div>
  );
}
