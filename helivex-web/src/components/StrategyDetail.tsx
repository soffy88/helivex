/**
 * StrategyDetail — 策略详情页(点策略钻取)
 * 6 子视图:持仓 / 交易历史 / 资金曲线 / 信号历史 / 统计 / 执行质量
 * 全部真实数据(detailApi)。无数据 → 诚实空状态,绝不填假数据。
 */
'use client';

import { useState } from 'react';
import { OEquityCurveChart } from '@helios/blocks';
import { SafeGateBadge } from './SafeBadges';
import { EmptyState } from './EmptyState';
import { detailApi } from '@/lib/api-client';
import { useApi } from '@/lib/use-api';
import type {
  StrategyState, Position, Trade, StrategyEquity, SignalLog, StrategyStats,
} from '@/types/api';

type SubView = 'positions' | 'trades' | 'equity' | 'signals' | 'stats' | 'execution';
const SUBVIEWS: { id: SubView; label: string }[] = [
  { id: 'positions', label: '持仓' }, { id: 'trades', label: '交易历史' },
  { id: 'equity', label: '资金曲线' }, { id: 'signals', label: '信号历史' },
  { id: 'stats', label: '统计' }, { id: 'execution', label: '执行质量' },
];

function Loading() { return <EmptyState text="加载中…" />; }
function ErrBox({ e }: { e: string }) { return <EmptyState text="加载失败" sub={e} />; }

export function StrategyDetail({ strategy, onBack }: { strategy: StrategyState; onBack: () => void }) {
  const [view, setView] = useState<SubView>('signals');
  const id = strategy.strategy_id;
  return (
    <div className="hv-detail-page">
      <div className="hv-detail-header">
        <button className="hv-back-btn" onClick={onBack}>← 返回</button>
        <h2 className="hv-detail-title">{strategy.name}</h2>
        <span className="hv-mode-badge">{strategy.mode}</span>
        <SafeGateBadge verdict={strategy.gate?.verdict} dsr={strategy.gate?.dsr} pbo={strategy.gate?.pbo} compact />
      </div>
      <div className="hv-subview-tabs">
        {SUBVIEWS.map(s => (
          <button key={s.id} className="hv-subview-tab" data-active={view === s.id ? 'true' : undefined}
            onClick={() => setView(s.id)}>{s.label}</button>
        ))}
      </div>
      <div className="hv-subview-body">
        {view === 'positions' && <PositionsView id={id} />}
        {view === 'trades' && <TradesView id={id} />}
        {view === 'equity' && <EquityView id={id} />}
        {view === 'signals' && <SignalsView id={id} />}
        {view === 'stats' && <StatsView id={id} />}
        {view === 'execution' && <ExecutionView id={id} />}
      </div>
    </div>
  );
}

const num = (v: number | null | undefined, d = 4) => v === null || v === undefined ? '—' : v.toFixed(d);

function PositionsView({ id }: { id: string }) {
  const { data, loading, error } = useApi<Position[]>(() => detailApi.positions(id), [id], 15000);
  if (loading) return <Loading />; if (error) return <ErrBox e={error} />;
  const rows = data ?? [];
  if (rows.length === 0) return <EmptyState text="当前无持仓" />;
  return (
    <table className="hv-table">
      <thead><tr><th>品种</th><th>方向</th><th>数量</th><th>开仓价</th><th>现价</th><th>未实现盈亏</th><th>时长</th><th>杠杆</th><th>强平价</th></tr></thead>
      <tbody>{rows.map((p, i) => (
        <tr key={i}>
          <td>{p.instrument}</td>
          <td style={{ color: p.side === 'long' ? 'var(--success,#3fb950)' : 'var(--destructive)' }}>{p.side}</td>
          <td className="hv-num">{p.quantity}</td><td className="hv-num">{num(p.avg_entry_price)}</td><td className="hv-num">{num(p.current_price)}</td>
          <td className="hv-num" style={{ color: (p.unrealized_pnl ?? 0) >= 0 ? 'var(--success,#3fb950)' : 'var(--destructive)' }}>
            {num(p.unrealized_pnl)} ({num(p.unrealized_pnl_pct, 2)}%)
          </td>
          <td>{p.holding_duration ?? '—'}</td><td className="hv-num">{p.leverage ?? '—'}</td>
          <td className="hv-num" style={{ color: 'var(--destructive)' }}>{p.liquidation_price ?? '—'}</td>
        </tr>
      ))}</tbody>
    </table>
  );
}

function TradesView({ id }: { id: string }) {
  const { data, loading, error } = useApi<Trade[]>(() => detailApi.trades(id), [id]);
  if (loading) return <Loading />; if (error) return <ErrBox e={error} />;
  const rows = data ?? [];
  if (rows.length === 0) return <EmptyState text="暂无已平仓交易" sub="持仓平仓后形成完整 round-trip 才计入" />;
  return (
    <table className="hv-table">
      <thead><tr><th>开仓</th><th>平仓</th><th>品种</th><th>方向</th><th>盈亏</th><th>手续费</th><th>触发信号</th><th>平仓原因</th></tr></thead>
      <tbody>{rows.map(t => (
        <tr key={t.trade_id}>
          <td>{t.open_time}</td><td>{t.close_time}</td><td>{t.instrument}</td><td>{t.side}</td>
          <td className="hv-num" style={{ color: t.realized_pnl >= 0 ? 'var(--success,#3fb950)' : 'var(--destructive)' }}>{t.realized_pnl}</td>
          <td className="hv-num">{t.fees}</td><td>{t.trigger_signal}</td><td>{t.exit_reason}</td>
        </tr>
      ))}</tbody>
    </table>
  );
}

function EquityView({ id }: { id: string }) {
  const { data, loading, error } = useApi<StrategyEquity>(() => detailApi.equity(id), [id], 30000);
  if (loading) return <Loading />; if (error) return <ErrBox e={error} />;
  const pts = data?.points ?? [];
  if (pts.length < 2) return <EmptyState text="数据不足" sub="需 ≥2 个成交点" />;
  return (
    <div className="hv-chart-box">
      <OEquityCurveChart points={pts.map(p => ({ date: p.date, equity: p.equity, drawdown: p.drawdown }))} showDrawdown />
    </div>
  );
}

function SignalsView({ id }: { id: string }) {
  const { data, loading, error } = useApi<SignalLog[]>(() => detailApi.signals(id), [id], 15000);
  const [expanded, setExpanded] = useState<number | null>(null);
  if (loading) return <Loading />; if (error) return <ErrBox e={error} />;
  const rows = data ?? [];
  if (rows.length === 0) return <EmptyState text="暂无信号" />;
  const DIR_COLOR: Record<string, string> = { long: 'var(--success,#3fb950)', short: 'var(--destructive)', neutral: 'var(--muted-foreground)' };
  return (
    <div className="hv-signal-list">
      {rows.map((s, i) => (
        <div key={i} className="hv-signal-item">
          <button className="hv-signal-head" onClick={() => setExpanded(e => e === i ? null : i)}>
            <span className="hv-signal-time">{new Date(s.time).toLocaleString()}</span>
            <span className="hv-signal-dir" style={{ color: DIR_COLOR[s.direction] }}>{s.direction}</span>
            <span className="hv-signal-strength">强度 {s.strength}</span>
            <span className="hv-signal-acted" data-acted={s.acted ? 'true' : undefined}>{s.acted ? '已下单' : '未下单'}</span>
            <span className="hv-expand-icon">{expanded === i ? '▾' : '▸'}</span>
          </button>
          {expanded === i && (
            <div className="hv-signal-indicators">
              {(s.indicator_values ?? []).map((iv, j) => (
                <div key={j} className="hv-indicator-snap">
                  <span className="hv-indicator-name">{iv.name}</span><span className="hv-indicator-val">{iv.value}</span>
                </div>
              ))}
            </div>
          )}
        </div>
      ))}
    </div>
  );
}

function StatsView({ id }: { id: string }) {
  const { data, loading, error } = useApi<StrategyStats>(() => detailApi.stats(id), [id], 30000);
  if (loading) return <Loading />; if (error) return <ErrBox e={error} />;
  const s = data;
  if (!s || s.total_trades === 0) return <EmptyState text="暂无交易统计" sub="等样本积累" />;
  return (
    <div>
      {!s.sample_sufficient && <div className="hv-honest-note">⚠️ 样本不足(n&lt;30),统计不显著。实盘 forward 是 gate 的终极裁决。</div>}
      <div className="hv-grid-4">
        <Metric label="总交易" val={s.total_trades} />
        <Metric label="胜率" val={`${(s.win_rate * 100).toFixed(0)}%`} />
        <Metric label="盈亏比" val={s.profit_factor.toFixed(2)} />
        <Metric label="Forward Sharpe" val={s.forward_sharpe.toFixed(2)} />
      </div>
      <div className="hv-honest-note" style={{ marginTop: 12 }}>
        实盘 forward Sharpe {s.forward_sharpe.toFixed(2)} vs backtest OOS {s.backtest_oos_sharpe?.toFixed(2) ?? '—'} — forward 是终极裁决。
      </div>
    </div>
  );
}

function ExecutionView({ id }: { id: string }) {
  const { data, loading, error } = useApi<{ fills: { fill_id: string; time: string; instrument: string; expected_price: number; actual_price: number; liquidity: string }[] }>(
    () => detailApi.execution(id) as unknown as Promise<{ fills: { fill_id: string; time: string; instrument: string; expected_price: number; actual_price: number; liquidity: string }[] }>, [id], 15000);
  if (loading) return <Loading />; if (error) return <ErrBox e={error} />;
  const fills = data?.fills ?? [];
  if (fills.length === 0) return <EmptyState text="暂无成交" sub="等首笔 fill" />;
  const slips = fills.map(f => Math.abs((f.actual_price - f.expected_price) / f.expected_price * 1e4));
  const avg = slips.reduce((a, b) => a + b, 0) / slips.length;
  const mx = Math.max(...slips);
  const takers = fills.filter(f => f.liquidity === 'taker').length;
  return (
    <div>
      <div className="hv-grid-4">
        <Metric label="成交数" val={fills.length} />
        <Metric label="平均滑点" val={`${avg.toFixed(1)} bps`} />
        <Metric label="最大滑点" val={`${mx.toFixed(1)} bps`} />
        <Metric label="taker 占比" val={`${(takers / fills.length * 100).toFixed(0)}%`} />
      </div>
      <div className="hv-honest-note" style={{ marginTop: 12 }}>backtest 假设 ~2bps maker;真实成交以上为准(maker/taker 来自交易所成交回报)。</div>
      <table className="hv-table" style={{ marginTop: 12 }}>
        <thead><tr><th>时间</th><th>品种</th><th>信号价</th><th>真实价</th><th>滑点</th><th>类型</th></tr></thead>
        <tbody>{fills.slice(0, 50).map(f => (
          <tr key={f.fill_id}>
            <td>{new Date(f.time).toLocaleString()}</td><td>{f.instrument}</td>
            <td className="hv-num">{f.expected_price}</td><td className="hv-num">{f.actual_price}</td>
            <td className="hv-num">{Math.abs((f.actual_price - f.expected_price) / f.expected_price * 1e4).toFixed(1)} bps</td>
            <td>{f.liquidity}</td>
          </tr>
        ))}</tbody>
      </table>
    </div>
  );
}

function Metric({ label, val }: { label: string; val: string | number }) {
  return (
    <div className="hv-metric-card">
      <span className="hv-metric-label">{label}</span>
      <span className="hv-metric-val">{val}</span>
    </div>
  );
}
