/**
 * PortfolioTab — 全局组合视图(真实数据)
 * 真实 summary + correlation + 合并资金曲线 + 全局 kill switch
 */
'use client';

import { useState } from 'react';
import { EmptyState, Skeleton, StaleBanner } from '../EmptyState';
import { EquityPanel, Underwater, DivergingBars } from '../charts';
import { portfolioApi } from '@/lib/api-client';
import { useApi } from '@/lib/use-api';
import type { PortfolioSummary, CorrelationMatrix, PortfolioEquity, PortfolioAttributionResp } from '@/types/api';

const shortStrat = (s: string) => s.replace(/_usdt_swap_okx$/, '').replace(/_/g, ' ');

export function PortfolioTab() {
  const { data, loading, error, stale } = useApi(
    () => Promise.all([portfolioApi.summary(), portfolioApi.correlation(), portfolioApi.equity()]),
    [], 15000, 'portfolio',
  );
  const attr = useApi<PortfolioAttributionResp>(() => portfolioApi.attribution(), [], 30000, 'portfolio-attr');
  const [killConfirm, setKillConfirm] = useState(false);
  const [killed, setKilled] = useState(false);

  const corrColor = (v: number) => {
    if (v >= 0.99) return 'var(--muted)';
    const abs = Math.abs(v);
    return abs < 0.3 ? 'color-mix(in oklch, var(--success, oklch(0.62 0.18 145)) 30%, transparent)'
      : abs < 0.6 ? 'color-mix(in oklch, oklch(0.70 0.15 80) 30%, transparent)'
      : 'color-mix(in oklch, var(--destructive) 30%, transparent)';
  };

  if (loading && !data) return <div className="hv-tab"><Skeleton /></div>;
  if (error && !data) return <div className="hv-tab"><EmptyState text="网关连接失败" sub={error} /></div>;
  const [sum, corr, eq] = data as [PortfolioSummary, CorrelationMatrix, PortfolioEquity];
  const pts = eq?.combined ?? [];

  const doKill = async () => {
    try { await portfolioApi.kill(); setKilled(true); } catch { /* surfaced below */ }
    setKillConfirm(false);
  };

  return (
    <div className="hv-tab">
      {stale && <StaleBanner error={error!} />}
      <div className="hv-section-title">组合总览</div>
      <div className="hv-grid-4">
        <div className="hv-metric-card"><span className="hv-metric-label">总持仓</span><span className="hv-metric-val">{sum?.total_positions ?? '—'}</span></div>
        <div className="hv-metric-card"><span className="hv-metric-label">总未实现盈亏</span>
          <span className="hv-metric-val" style={{ color: (sum?.total_unrealized_pnl ?? 0) >= 0 ? 'var(--success,#3fb950)' : 'var(--destructive)' }}>${sum?.total_unrealized_pnl ?? '—'}</span></div>
        <div className="hv-metric-card"><span className="hv-metric-label">已实现盈亏</span>
          <span className="hv-metric-val" style={{ color: (sum?.total_realized_pnl ?? 0) >= 0 ? 'var(--success,#3fb950)' : 'var(--destructive)' }}>${sum?.total_realized_pnl ?? '—'}</span></div>
        <div className="hv-metric-card"><span className="hv-metric-label">可用资金</span><span className="hv-metric-val">${sum?.available?.toLocaleString() ?? '—'}</span></div>
      </div>

      {pts.length < 2 ? <EmptyState text="数据不足" sub="需 ≥2 个成交点" /> : (
        <>
          <div className="hv-chart-box"><EquityPanel pts={pts} title="合并资金曲线" h={260} /></div>
          <div className="hv-section-title">回撤水下图</div>
          <div className="hv-chart-box"><Underwater pts={pts.map(p => p.drawdown ?? 0)} /></div>
        </>
      )}

      <div className="hv-section-title">策略相关性(低相关 = 分散好)</div>
      {!corr?.matrix?.length ? <EmptyState text="暂无相关性数据" /> : (
        <div className="hv-corr-matrix">
          <table className="hv-table" aria-label="策略相关性矩阵">
            <thead><tr><th></th>{corr.strategies.map(s => <th key={s} className="hv-num">{shortStrat(s)}</th>)}</tr></thead>
            <tbody>
              {corr.matrix.map((row, i) => (
                <tr key={i}>
                  <td>{corr.strategies[i] ? shortStrat(corr.strategies[i]) : i}</td>
                  {row.map((v, j) => <td key={j} className="hv-num" style={{ background: corrColor(v), textAlign: 'center' }}>{v.toFixed(2)}</td>)}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      <div className="hv-honest-note">低相关性利于组合分散。边界策略组合可能整体过 gate(R4)。</div>

      {/* P&L 归因(每策略,真实成交派生)— 原 P&L tab 并入此处 */}
      <div className="hv-section-title">
        P&L 归因(按策略)· 合计已实现 {attr.data ? `$${attr.data.total_realized.toFixed(2)}` : '—'}
      </div>
      {(attr.data?.by_strategy ?? []).length === 0 ? <EmptyState text="暂无归因数据" sub="需已平仓的回合" /> : (
        <>
          <DivergingBars items={attr.data!.by_strategy.map(s => ({ label: shortStrat(s.strategy_id), value: s.realized_pnl, ok: s.realized_pnl >= 0 }))} unit="$" />
          <table className="hv-table" aria-label="P&L 归因">
            <thead><tr><th>策略</th><th>已实现 P&L</th><th>占毛额</th><th>成交数</th><th>胜率</th><th>单笔均益</th><th>最佳/最差</th></tr></thead>
            <tbody>
              {attr.data!.by_strategy.map(s => (
                <tr key={s.strategy_id}>
                  <td>{shortStrat(s.strategy_id)}</td>
                  <td style={{ color: s.realized_pnl >= 0 ? 'var(--success,#3fb950)' : 'var(--destructive)' }}>${s.realized_pnl.toFixed(2)}</td>
                  <td>{(s.pct_of_gross * 100).toFixed(1)}%</td>
                  <td>{s.n_trades}</td>
                  <td>{s.win_rate != null ? (s.win_rate * 100).toFixed(0) + '%' : '—'}</td>
                  <td>{s.avg_pnl != null ? '$' + s.avg_pnl.toFixed(3) : '—'}</td>
                  <td className="hv-num">{s.best != null ? `+${s.best}` : '—'} / {s.worst != null ? s.worst : '—'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      )}
      <div className="hv-honest-note">
        ⚠️ Paper 短期 P&L ≠ 策略有效。归因按<strong>策略</strong>(引擎级归因要等 enforce 后引擎驱动执行才成立)。
      </div>

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
