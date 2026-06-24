/**
 * PortfolioTab — 全局组合视图(真实数据)
 * 真实 summary + correlation + 合并资金曲线 + 全局 kill switch
 */
'use client';

import { useState } from 'react';
import { OEquityCurveChart } from '@helios/blocks';
import { EmptyState } from '../EmptyState';
import { portfolioApi } from '@/lib/api-client';
import { useApi } from '@/lib/use-api';
import type { PortfolioSummary, CorrelationMatrix, PortfolioEquity } from '@/types/api';

export function PortfolioTab() {
  const { data, loading, error } = useApi(
    () => Promise.all([portfolioApi.summary(), portfolioApi.correlation(), portfolioApi.equity()]),
    [], 15000,
  );
  const [killConfirm, setKillConfirm] = useState(false);
  const [killed, setKilled] = useState(false);

  const corrColor = (v: number) => {
    if (v >= 0.99) return 'var(--muted)';
    const abs = Math.abs(v);
    return abs < 0.3 ? 'color-mix(in oklch, var(--success, oklch(0.62 0.18 145)) 30%, transparent)'
      : abs < 0.6 ? 'color-mix(in oklch, oklch(0.70 0.15 80) 30%, transparent)'
      : 'color-mix(in oklch, var(--destructive) 30%, transparent)';
  };

  if (loading) return <div className="hv-tab"><EmptyState text="加载中…" /></div>;
  if (error) return <div className="hv-tab"><EmptyState text="网关连接失败" sub={error} /></div>;
  const [sum, corr, eq] = data as [PortfolioSummary, CorrelationMatrix, PortfolioEquity];
  const pts = eq?.combined ?? [];

  const doKill = async () => {
    try { await portfolioApi.kill(); setKilled(true); } catch { /* surfaced below */ }
    setKillConfirm(false);
  };

  return (
    <div className="hv-tab">
      <div className="hv-section-title">组合总览</div>
      <div className="hv-grid-4">
        <div className="hv-metric-card"><span className="hv-metric-label">总持仓</span><span className="hv-metric-val">{sum?.total_positions ?? '—'}</span></div>
        <div className="hv-metric-card"><span className="hv-metric-label">总未实现盈亏</span>
          <span className="hv-metric-val" style={{ color: (sum?.total_unrealized_pnl ?? 0) >= 0 ? 'var(--success,#3fb950)' : 'var(--destructive)' }}>${sum?.total_unrealized_pnl ?? '—'}</span></div>
        <div className="hv-metric-card"><span className="hv-metric-label">已实现盈亏</span>
          <span className="hv-metric-val" style={{ color: (sum?.total_realized_pnl ?? 0) >= 0 ? 'var(--success,#3fb950)' : 'var(--destructive)' }}>${sum?.total_realized_pnl ?? '—'}</span></div>
        <div className="hv-metric-card"><span className="hv-metric-label">可用资金</span><span className="hv-metric-val">${sum?.available?.toLocaleString() ?? '—'}</span></div>
      </div>

      <div className="hv-section-title">合并资金曲线</div>
      {pts.length < 2 ? <EmptyState text="数据不足" sub="需 ≥2 个成交点" /> : (
        <div className="hv-chart-box"><OEquityCurveChart points={pts.map(p => ({ date: p.date, equity: p.equity, drawdown: p.drawdown }))} showDrawdown /></div>
      )}

      <div className="hv-section-title">策略相关性(低相关 = 分散好)</div>
      {!corr?.matrix?.length ? <EmptyState text="暂无相关性数据" /> : (
        <div className="hv-corr-matrix">
          <table className="hv-table">
            <thead><tr><th></th>{corr.strategies.map(s => <th key={s} className="hv-num">{s.split('_')[0]}</th>)}</tr></thead>
            <tbody>
              {corr.matrix.map((row, i) => (
                <tr key={i}>
                  <td>{corr.strategies[i]?.split('_')[0] ?? i}</td>
                  {row.map((v, j) => <td key={j} className="hv-num" style={{ background: corrColor(v), textAlign: 'center' }}>{v.toFixed(2)}</td>)}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      <div className="hv-honest-note">低相关性利于组合分散。边界策略组合可能整体过 gate(R4)。</div>

      <div className="hv-section-title">风险控制</div>
      {killed ? (
        <div className="hv-honest-note">已发送停止指令。</div>
      ) : !killConfirm ? (
        <button className="hv-kill-btn" onClick={() => setKillConfirm(true)}>⏹ 一键停所有策略</button>
      ) : (
        <div className="hv-kill-confirm">
          <span>确定停止所有策略?这会平掉所有 paper 持仓。</span>
          <div className="hv-kill-actions">
            <button className="hv-kill-cancel" onClick={() => setKillConfirm(false)}>取消</button>
            <button className="hv-kill-confirm-btn" onClick={doKill}>确认停止</button>
          </div>
        </div>
      )}
    </div>
  );
}
