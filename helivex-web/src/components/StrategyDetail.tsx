/**
 * StrategyDetail — 策略详情页 6 子视图
 * 信号历史切真 (detailApi.signals), 其余诚实空状态等 fill
 */
'use client';

import { useState, useEffect } from 'react';
import { OEquityCurveChart, OExecutionFidelity, OGateBadge } from '@helios/blocks';
import { EmptyState } from './EmptyState';
import { detailApi } from '@/lib/api-client';
import type { StrategyState, SignalLog } from '@/types/api';

type SubView = 'positions' | 'trades' | 'equity' | 'signals' | 'stats' | 'execution';

const SUBVIEWS: { id: SubView; label: string }[] = [
  { id: 'positions', label: '持仓' },
  { id: 'trades', label: '交易历史' },
  { id: 'equity', label: '资金曲线' },
  { id: 'signals', label: '信号历史' },
  { id: 'stats', label: '统计' },
  { id: 'execution', label: '执行质量' },
];

export function StrategyDetail({ strategy, onBack }: { strategy: StrategyState; onBack: () => void }) {
  const [view, setView] = useState<SubView>('signals');

  return (
    <div className="hv-detail-page">
      <div className="hv-detail-header">
        <button className="hv-back-btn" onClick={onBack}>← 返回</button>
        <h2 className="hv-detail-title">{strategy.name}</h2>
        <span className="hv-mode-badge">{strategy.mode}</span>
        <OGateBadge verdict={strategy.gate.verdict} dsr={strategy.gate.dsr} pbo={strategy.gate.pbo} compact />
      </div>

      <div className="hv-subview-tabs">
        {SUBVIEWS.map(s => (
          <button key={s.id} className="hv-subview-tab"
            data-active={view === s.id ? 'true' : undefined}
            onClick={() => setView(s.id)}>{s.label}</button>
        ))}
      </div>

      <div className="hv-subview-body">
        {view === 'positions'  && <EmptyState text="当前无持仓" />}
        {view === 'trades'     && <EmptyState text="暂无历史交易" sub="等首笔 fill" />}
        {view === 'equity'     && <EmptyState text="净值 = 初始 5000" sub="等首笔成交" />}
        {view === 'signals'    && <SignalsView strategyId={strategy.strategy_id} />}
        {view === 'stats'      && <EmptyState text="暂无交易统计" sub="等样本积累 (n≥30)" />}
        {view === 'execution'  && <EmptyState text="暂无成交" sub="等首笔 fill" />}
      </div>
    </div>
  );
}

// §1.4 信号历史 — 切真
function SignalsView({ strategyId }: { strategyId: string }) {
  const [signals, setSignals] = useState<SignalLog[]>([]);
  const [loading, setLoading] = useState(true);
  const [expanded, setExpanded] = useState<number | null>(null);

  useEffect(() => {
    detailApi.signals(strategyId)
      .then((data: any) => {
        // gateway returns SignalLog[] directly
        const arr = Array.isArray(data) ? data : (data.signals ?? []);
        setSignals(arr);
      })
      .catch(console.error)
      .finally(() => setLoading(false));
  }, [strategyId]);

  if (loading) return <div className="hv-honest-note">加载信号…</div>;
  if (signals.length === 0) return <EmptyState text="暂无信号" />;

  const DIR_COLOR: Record<string, string> = {
    long: 'var(--success,#3fb950)', short: 'var(--destructive)', neutral: 'var(--muted-foreground)',
  };

  return (
    <div className="hv-signal-list">
      {signals.map((s, i) => (
        <div key={i} className="hv-signal-item">
          <button className="hv-signal-head" onClick={() => setExpanded(e => e === i ? null : i)}>
            <span className="hv-signal-time">{s.time}</span>
            <span className="hv-signal-dir" style={{ color: DIR_COLOR[s.direction] }}>{s.direction}</span>
            <span className="hv-signal-strength">强度 {s.strength.toFixed(2)}</span>
            <span className="hv-signal-acted" data-acted={s.acted ? 'true' : undefined}>
              {s.acted ? '已下单' : '未下单'}
            </span>
            {(s as any).instrument && <span className="hv-signal-inst">{(s as any).instrument}</span>}
            {(s as any).tier && <span className="hv-tier-badge" data-tier={(s as any).tier}>{(s as any).tier}</span>}
            <span className="hv-expand-icon">{expanded === i ? '▾' : '▸'}</span>
          </button>
          {expanded === i && (
            <div className="hv-signal-indicators">
              {s.indicator_values.length > 0 ? s.indicator_values.map((iv, j) => (
                <div key={j} className="hv-indicator-snap">
                  <span className="hv-indicator-name">{iv.name}</span>
                  <span className="hv-indicator-val">{String(iv.value)}</span>
                </div>
              )) : <span className="hv-muted">指标值将从下一个 bar 开始记录</span>}
              {(s as any).signal_price != null && (
                <div className="hv-indicator-snap">
                  <span className="hv-indicator-name">price</span>
                  <span className="hv-indicator-val">{(s as any).signal_price}</span>
                </div>
              )}
            </div>
          )}
        </div>
      ))}
    </div>
  );
}
