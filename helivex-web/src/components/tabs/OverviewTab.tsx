/**
 * OverviewTab — 平铺主页(一屏直达,无层层钻取)
 * 顶部全局概览条 + 四策略选择器 + 选中策略的 6 块数据全平铺。
 * 全部真实数据;无数据走诚实空状态;切策略各块自重拉(骨架屏)。
 */
'use client';

import { useState } from 'react';
import { SafeGateBadge } from '../SafeBadges';
import { EmptyState, Skeleton } from '../EmptyState';
import { helivexApi } from '@/lib/api-client';
import { useApi } from '@/lib/use-api';
import type { StrategyState, PaperAccount } from '@/types/api';
import {
  EquityView, StatsView, ExecutionView, PositionsView, TradesView, SignalsView,
} from '../StrategyViews';

interface ChainHealthReal { ok: boolean; n_total: number; n_gold: number; n_valid: number; }
interface Ledger { total_trials: number; history: { verdict: string }[]; }

const sign = (v: number) => (v > 0 ? 'var(--success,#3fb950)' : v < 0 ? 'var(--destructive)' : 'var(--muted-foreground)');
const usd = (v: number) => (v >= 0 ? '+' : '−') + '$' + Math.abs(v).toLocaleString(undefined, { maximumFractionDigits: 2 });

function Block({ title, children }: { title: string; children: React.ReactNode }) {
  return (<><div className="hv-section-title">{title}</div>{children}</>);
}

export function OverviewTab() {
  const { data, loading, error } = useApi(
    () => Promise.all([
      helivexApi.strategies(),
      helivexApi.account(),
      helivexApi.chainHealth() as unknown as Promise<ChainHealthReal>,
      helivexApi.gateTrials() as unknown as Promise<Ledger>,
    ]),
    [], 15000,
  );
  const [sel, setSel] = useState<string | null>(null);

  if (loading) return <div className="hv-tab"><Skeleton /></div>;
  if (error) return <div className="hv-tab"><EmptyState text="网关连接失败" sub={error} /></div>;
  const [strategies, account, chain, ledger] = data as [StrategyState[], PaperAccount, ChainHealthReal, Ledger];

  const id = sel ?? strategies?.[0]?.strategy_id ?? null;
  const cur = strategies?.find(s => s.strategy_id === id) ?? null;
  const passes = (ledger?.history ?? []).filter(t => t.verdict?.toUpperCase() === 'PASS').length;
  const total = ledger?.history?.length ?? 0;

  return (
    <div className="hv-tab">
      {/* ── 顶部:全局概览条(收窄) ── */}
      <div className="hv-bar">
        <div className="hv-bar-item"><span className="hv-bar-label">余额</span><span className="hv-bar-val">${account?.balance?.toLocaleString(undefined, { maximumFractionDigits: 2 }) ?? '—'}</span></div>
        <div className="hv-bar-item"><span className="hv-bar-label">今日 net P&L</span><span className="hv-bar-val" style={{ color: sign(account?.pnl_today_net ?? 0) }}>{account ? usd(account.pnl_today_net) : '—'}</span></div>
        <div className="hv-bar-item"><span className="hv-bar-label">gross P&L</span><span className="hv-bar-val" style={{ color: sign(account?.pnl_today_gross ?? 0) }}>{account ? usd(account.pnl_today_gross) : '—'}</span></div>
        <div className="hv-bar-item"><span className="hv-bar-label">审计链</span><span className="hv-bar-val" style={{ color: chain?.ok ? 'var(--success,#3fb950)' : 'var(--destructive)' }}>{chain?.ok ? '✓' : '✕'} GOLD {chain?.n_gold ?? 0}/{chain?.n_total ?? 0}</span></div>
        <div className="hv-bar-item"><span className="hv-bar-label">Gate 账本</span><span className="hv-bar-val">{passes}/{total} PASS</span></div>
        <span className="hv-bar-note">Paper 短期 P&L ≠ 策略有效;gate 全 FAIL/NO-GO。</span>
      </div>

      {/* ── 中部:四策略选择器 ── */}
      <div className="hv-sel-row">
        {(strategies ?? []).map(s => (
          <button key={s.strategy_id} className="hv-sel-chip" data-active={id === s.strategy_id ? 'true' : undefined}
            onClick={() => setSel(s.strategy_id)}>
            <span className="hv-strat-name">{s.name}</span>
            <span className="hv-sel-meta">
              <SafeGateBadge verdict={s.gate?.verdict} dsr={s.gate?.dsr} pbo={s.gate?.pbo} compact />
              <span className="hv-sel-pos">{s.position} · 今日 {s.signals_today}</span>
            </span>
          </button>
        ))}
      </div>

      {/* ── 下部:选中策略的 6 块平铺 ── */}
      {!id || !cur ? <EmptyState text="暂无策略" /> : (
        <div className="hv-flat" key={id}>
          <Block title={`资金曲线 — ${cur.name}`}><EquityView id={id} /></Block>
          <div className="hv-grid-2">
            <div className="hv-flat-col"><div className="hv-section-title">统计</div><StatsView id={id} /></div>
            <div className="hv-flat-col"><div className="hv-section-title">执行质量(真实滑点 / maker-taker / 延迟)</div><ExecutionView id={id} /></div>
          </div>
          <Block title="持仓"><PositionsView id={id} /></Block>
          <Block title="交易历史"><TradesView id={id} /></Block>
          <Block title="信号历史(真实 signal + indicator)"><SignalsView id={id} /></Block>
        </div>
      )}
    </div>
  );
}
