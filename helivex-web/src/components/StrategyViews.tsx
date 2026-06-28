/**
 * StrategyViews — 单策略的 6 块数据视图(真实 detailApi,无 mock)。
 * 复用于新版平铺主页(OverviewTab)。每块各自拉取、各自 loading/empty。
 */
'use client';

import { useState } from 'react';
import { OEquityCurveChart } from '@helios/blocks';
import { EmptyState, Skeleton } from './EmptyState';
import { detailApi } from '@/lib/api-client';
import { useApi } from '@/lib/use-api';
import type { Position, Trade, StrategyEquity, SignalLog, StrategyStats } from '@/types/api';

const num = (v: number | null | undefined, d = 4) => v === null || v === undefined ? '—' : v.toFixed(d);
function Loading() { return <Skeleton />; }
function ErrBox({ e }: { e: string }) { return <EmptyState text="加载失败" sub={e} />; }

export function Metric({ label, val }: { label: string; val: string | number }) {
  return (
    <div className="hv-metric-card">
      <span className="hv-metric-label">{label}</span>
      <span className="hv-metric-val">{val}</span>
    </div>
  );
}

export function PositionsView({ id }: { id: string }) {
  const { data, loading, error } = useApi<Position[]>(() => detailApi.positions(id), [id], 15000, `pos:${id}`);
  if (loading && !data) return <Loading />; if (error && !data) return <ErrBox e={error} />;
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

export function TradesView({ id }: { id: string }) {
  const { data, loading, error } = useApi<Trade[]>(() => detailApi.trades(id), [id], undefined, `trades:${id}`);
  if (loading && !data) return <Loading />; if (error && !data) return <ErrBox e={error} />;
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

export function EquityView({ id }: { id: string }) {
  const { data, loading, error } = useApi<StrategyEquity>(() => detailApi.equity(id), [id], 30000, `eq:${id}`);
  if (loading && !data) return <Loading />; if (error && !data) return <ErrBox e={error} />;
  const pts = data?.points ?? [];
  if (pts.length < 2) return <EmptyState text="数据不足" sub="需 ≥2 个成交点" />;
  return (
    <div className="hv-chart-box">
      <OEquityCurveChart points={pts.map(p => ({ date: p.date, equity: p.equity, drawdown: p.drawdown }))} showDrawdown />
    </div>
  );
}

export function SignalsView({ id }: { id: string }) {
  const { data, loading, error } = useApi<SignalLog[]>(() => detailApi.signals(id), [id], 15000, `sig:${id}`);
  const [expanded, setExpanded] = useState<number | null>(null);
  if (loading && !data) return <Loading />; if (error && !data) return <ErrBox e={error} />;
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

export function StatsView({ id }: { id: string }) {
  const { data, loading, error } = useApi<StrategyStats>(() => detailApi.stats(id), [id], 30000, `stats:${id}`);
  if (loading && !data) return <Loading />; if (error && !data) return <ErrBox e={error} />;
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
        <Metric label="最大回撤" val={`${(s.max_drawdown * 100).toFixed(0)}%`} />
        <Metric label="累计盈亏" val={`$${s.total_pnl?.toFixed?.(4) ?? s.total_pnl}`} />
      </div>
    </div>
  );
}

interface ExecFill { fill_id: string; time: string; instrument: string; expected_price: number; actual_price: number; liquidity: string }
export function ExecutionView({ id }: { id: string }) {
  const { data, loading, error } = useApi<{ fills: ExecFill[] }>(
    () => detailApi.execution(id) as unknown as Promise<{ fills: ExecFill[] }>, [id], 15000, `exec:${id}`);
  if (loading && !data) return <Loading />; if (error && !data) return <ErrBox e={error} />;
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
      <div className="hv-honest-note" style={{ marginTop: 12 }}>backtest 假设 ~2bps maker;真实成交以上为准(maker/taker 来自交易所回报)。</div>
    </div>
  );
}
