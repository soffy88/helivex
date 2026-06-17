/**
 * StrategyDetail — 策略详情页(§1,点策略钻取)
 * 6 子视图:持仓 / 交易历史 / 资金曲线 / 信号历史 / 统计 / 执行质量
 *
 * 诚实空状态(§5):paper fills=0 时各视图显示空,不填假数据。
 * 信号历史现在有真数据,先填。
 */
'use client';

import { useState } from 'react';
import { OEquityCurveChart, OExecutionFidelity } from '@helios/blocks';
import { SafeGateBadge } from './SafeBadges';
import { EmptyState } from './EmptyState';
import {
  MOCK_POSITIONS, MOCK_TRADES, MOCK_EQUITY, MOCK_SIGNALS, MOCK_STATS, MOCK_EXECUTION,
} from '@/lib/mock-data';
import type { StrategyState } from '@/types/api';

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
  const [view, setView] = useState<SubView>('signals'); // 默认信号(现在有数据)

  return (
    <div className="hv-detail-page">
      <div className="hv-detail-header">
        <button className="hv-back-btn" onClick={onBack}>← 返回</button>
        <h2 className="hv-detail-title">{strategy.name}</h2>
        <span className="hv-mode-badge">{strategy.mode}</span>
        <SafeGateBadge verdict={strategy.gate.verdict} dsr={strategy.gate.dsr} pbo={strategy.gate.pbo} compact />
      </div>

      <div className="hv-subview-tabs">
        {SUBVIEWS.map(s => (
          <button key={s.id} className="hv-subview-tab"
            data-active={view === s.id ? 'true' : undefined}
            onClick={() => setView(s.id)}>{s.label}</button>
        ))}
      </div>

      <div className="hv-subview-body">
        {view === 'positions' && <PositionsView />}
        {view === 'trades' && <TradesView />}
        {view === 'equity' && <EquityView />}
        {view === 'signals' && <SignalsView />}
        {view === 'stats' && <StatsView />}
        {view === 'execution' && <ExecutionView />}
      </div>
    </div>
  );
}

// §1.1 持仓
function PositionsView() {
  if (MOCK_POSITIONS.length === 0) return <EmptyState text="当前无持仓" />;
  return (
    <table className="hv-table">
      <thead><tr><th>品种</th><th>方向</th><th>数量</th><th>开仓价</th><th>现价</th><th>未实现盈亏</th><th>时长</th><th>杠杆</th><th>强平价</th></tr></thead>
      <tbody>{MOCK_POSITIONS.map((p, i) => (
        <tr key={i}>
          <td>{p.instrument}</td>
          <td style={{ color: p.side === 'long' ? 'var(--success,#3fb950)' : 'var(--destructive)' }}>{p.side}</td>
          <td className="hv-num">{p.quantity}</td><td className="hv-num">{p.avg_entry_price}</td><td className="hv-num">{p.current_price}</td>
          <td className="hv-num" style={{ color: p.unrealized_pnl >= 0 ? 'var(--success,#3fb950)' : 'var(--destructive)' }}>
            {p.unrealized_pnl} ({p.unrealized_pnl_pct}%)
          </td>
          <td>{p.holding_duration}</td><td className="hv-num">{p.leverage ?? '—'}</td>
          <td className="hv-num" style={{ color: 'var(--destructive)' }}>{p.liquidation_price ?? '—'}</td>
        </tr>
      ))}</tbody>
    </table>
  );
}

// §1.2 交易历史
function TradesView() {
  if (MOCK_TRADES.length === 0) return <EmptyState text="暂无历史交易" sub="等首笔 fill" />;
  return (
    <div>
      <div className="hv-trade-filters">
        <select className="hv-select"><option>全部品种</option></select>
        <select className="hv-select"><option>全部盈亏</option><option>只看盈</option><option>只看亏</option></select>
        <button className="hv-export-btn">导出 CSV</button>
      </div>
      <table className="hv-table">
        <thead><tr><th>开仓</th><th>平仓</th><th>品种</th><th>方向</th><th>盈亏</th><th>手续费</th><th>触发信号</th><th>平仓原因</th></tr></thead>
        <tbody>{MOCK_TRADES.map(t => (
          <tr key={t.trade_id}>
            <td>{t.open_time}</td><td>{t.close_time}</td><td>{t.instrument}</td><td>{t.side}</td>
            <td className="hv-num" style={{ color: t.realized_pnl >= 0 ? 'var(--success,#3fb950)' : 'var(--destructive)' }}>{t.realized_pnl}</td>
            <td className="hv-num">{t.fees}</td><td>{t.trigger_signal}</td><td>{t.exit_reason}</td>
          </tr>
        ))}</tbody>
      </table>
    </div>
  );
}

// §1.3 资金曲线
function EquityView() {
  const hasData = MOCK_EQUITY.points.length > 1;
  if (!hasData) return <EmptyState text="净值 = 初始 5000" sub="等首笔成交" />;
  return (
    <div className="hv-chart-box">
      <OEquityCurveChart points={MOCK_EQUITY.points.map(p => ({ date: p.date, equity: p.equity, drawdown: p.drawdown }))} showDrawdown />
    </div>
  );
}

// §1.4 信号历史(有真数据)
function SignalsView() {
  const [expanded, setExpanded] = useState<number | null>(null);
  if (MOCK_SIGNALS.length === 0) return <EmptyState text="暂无信号" />;
  const DIR_COLOR: Record<string, string> = {
    long: 'var(--success,#3fb950)', short: 'var(--destructive)', neutral: 'var(--muted-foreground)',
  };
  return (
    <div className="hv-signal-list">
      {MOCK_SIGNALS.map((s, i) => (
        <div key={i} className="hv-signal-item">
          <button className="hv-signal-head" onClick={() => setExpanded(e => e === i ? null : i)}>
            <span className="hv-signal-time">{s.time}</span>
            <span className="hv-signal-dir" style={{ color: DIR_COLOR[s.direction] }}>{s.direction}</span>
            <span className="hv-signal-strength">强度 {s.strength}</span>
            <span className="hv-signal-acted" data-acted={s.acted ? 'true' : undefined}>
              {s.acted ? '已下单' : '未下单'}
            </span>
            <span className="hv-expand-icon">{expanded === i ? '▾' : '▸'}</span>
          </button>
          {expanded === i && (
            <div className="hv-signal-indicators">
              {s.indicator_values.map((iv, j) => (
                <div key={j} className="hv-indicator-snap">
                  <span className="hv-indicator-name">{iv.name}</span>
                  <span className="hv-indicator-val">{iv.value}</span>
                </div>
              ))}
            </div>
          )}
        </div>
      ))}
    </div>
  );
}

// §1.5 统计
function StatsView() {
  const s = MOCK_STATS;
  if (s.total_trades === 0) {
    return <EmptyState text="暂无交易统计" sub="等样本积累" />;
  }
  return (
    <div>
      {!s.sample_sufficient && (
        <div className="hv-honest-note">⚠️ 样本不足(n&lt;30),统计不显著。实盘 forward 是 gate 的终极裁决。</div>
      )}
      <div className="hv-grid-4">
        <Metric label="总交易" val={s.total_trades} />
        <Metric label="胜率" val={`${(s.win_rate * 100).toFixed(0)}%`} />
        <Metric label="盈亏比" val={s.profit_factor.toFixed(2)} />
        <Metric label="Forward Sharpe" val={s.forward_sharpe.toFixed(2)} />
      </div>
      <div className="hv-honest-note" style={{ marginTop: 12 }}>
        实盘 forward Sharpe {s.forward_sharpe.toFixed(2)} vs backtest OOS {s.backtest_oos_sharpe?.toFixed(2)} — forward 是终极裁决。
      </div>
    </div>
  );
}

// §1.6 执行质量
function ExecutionView() {
  const e = MOCK_EXECUTION;
  if (e.fills.length === 0) return <EmptyState text="暂无成交" sub="等首笔 fill" />;
  return (
    <OExecutionFidelity metrics={[
      { label: '平均滑点 (bps)', backtest: e.backtest_assumed_bps, actual: e.avg_slippage_bps, worseWhenHigher: true },
      { label: '最大滑点 (bps)', backtest: e.backtest_assumed_bps, actual: e.max_slippage_bps, worseWhenHigher: true },
    ]} />
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
