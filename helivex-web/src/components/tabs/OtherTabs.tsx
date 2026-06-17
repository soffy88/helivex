/**
 * 其余 Tab:Strategies / Backtest / Executions / P&L / Audit
 */
'use client';

import { useState } from 'react';
import { OWalkForwardChart, OExecutionFidelity, OEquityCurveChart } from '@helios/blocks';
import { SafeGateBadge, SafeRegimeBadge } from '../SafeBadges';
import {
  MOCK_STRATEGIES, MOCK_BACKTEST, MOCK_EXECUTIONS, MOCK_DECISIONS,
} from '@/lib/mock-data';

// ── Strategies Tab ──────────────────────────────
export function StrategiesTab({ onDrill }: { onDrill?: (id: string) => void }) {
  const [expanded, setExpanded] = useState<string | null>('trend_dual');
  return (
    <div className="hv-tab">
      {(MOCK_STRATEGIES ?? []).map(s => (
        <div key={s.strategy_id} className="hv-strat-detail">
          <button className="hv-strat-detail__head" onClick={() => setExpanded(e => e === s.strategy_id ? null : s.strategy_id)}>
            <span className="hv-strat-name">{s.name}</span>
            <SafeRegimeBadge regime={s.regime} compact />
            <SafeGateBadge verdict={s.gate?.verdict} dsr={s.gate?.dsr} pbo={s.gate?.pbo} compact />
            <span className="hv-mode-badge">{s.mode}</span>
            <span className="hv-expand-icon">{expanded === s.strategy_id ? '▾' : '▸'}</span>
          </button>
          {expanded === s.strategy_id && (
            <div className="hv-strat-detail__body">
              <div className="hv-detail-row"><span>生效配置:</span> {s.indicators.filter(i => i.enabled).map(i => i.name).join(', ')}</div>
              <div className="hv-detail-row"><span>signal:</span> <code>{s.signal_logic.entry}</code></div>
              <div className="hv-detail-row"><span>持仓:</span> {s.position}</div>
              <div className="hv-detail-row"><span>gate:</span> <SafeGateBadge verdict={s.gate?.verdict} dsr={s.gate?.dsr} pbo={s.gate?.pbo} /></div>
              <button className="hv-drill-btn" onClick={() => onDrill?.(s.strategy_id)}>查看详情(持仓/交易/资金/信号/统计/执行)→</button>
            </div>
          )}
        </div>
      ))}
    </div>
  );
}

// ── Backtest Tab ────────────────────────────────
export function BacktestTab() {
  const bt = MOCK_BACKTEST;
  return (
    <div className="hv-tab">
      <div className="hv-section-title">Backtest 运行器</div>
      <div className="hv-bt-controls">
        <select className="hv-select"><option>策略1 趋势双向</option></select>
        <select className="hv-select"><option>BTC-USDT</option></select>
        <select className="hv-select"><option>2025-01 ~ 2026-06</option></select>
        <button className="hv-run-gate">Run Backtest</button>
      </div>

      <div className="hv-grid-4">
        <div className="hv-metric-card"><span className="hv-metric-label">Sharpe</span><span className="hv-metric-val">{bt.sharpe}</span></div>
        <div className="hv-metric-card"><span className="hv-metric-label">Max DD</span><span className="hv-metric-val hv-neg">{(bt.max_drawdown * 100).toFixed(0)}%</span></div>
        <div className="hv-metric-card"><span className="hv-metric-label">Fills</span><span className="hv-metric-val">{bt.fill_count}</span></div>
        <div className="hv-metric-card"><span className="hv-metric-label">年化</span><span className="hv-metric-val">{(bt.annualized * 100).toFixed(0)}%</span></div>
      </div>

      <div className="hv-section-title">Walk-Forward(fold 方差是 FAIL 主因)</div>
      <OWalkForwardChart folds={bt.folds} threshold={0} />

      <div className="hv-section-title">Regime 分段表现(诚实诊断)</div>
      <div className="hv-grid-3">
        {bt.regime_breakdown.map(r => (
          <div key={r.regime} className="hv-metric-card">
            <SafeRegimeBadge regime={r.regime} />
            <span className="hv-metric-val" style={{ color: r.sharpe >= 0 ? 'var(--success,#3fb950)' : 'var(--destructive)' }}>
              {r.sharpe >= 0 ? '+' : ''}{r.sharpe}
            </span>
          </div>
        ))}
      </div>
      <div className="hv-honest-note">策略只在 trend regime 有效(+1.2),chop/bear 为负。这是真实 regime 依赖。</div>
    </div>
  );
}

// ── Executions Tab ──────────────────────────────
export function ExecutionsTab() {
  const fidelity = [
    { label: '平均滑点 (bps)', backtest: 2, actual: 7.3, worseWhenHigher: true },
    { label: 'maker fill rate', backtest: 95, actual: 71, unit: '%', worseWhenHigher: false },
    { label: 'funding (bps/8h)', backtest: 1.0, actual: 1.4, worseWhenHigher: true },
    { label: '拒单率', backtest: 0, actual: 2.1, unit: '%', worseWhenHigher: true },
  ];
  return (
    <div className="hv-tab">
      <div className="hv-section-title">执行真实度(backtest vs 真实,决定 backtest 可信度)</div>
      <OExecutionFidelity metrics={fidelity} />
      <div className="hv-honest-note">真实滑点是 backtest 假设的 3.6 倍,fill rate 低 25%。backtest 偏乐观,P&L 需打折看。</div>

      <div className="hv-section-title">Fill 列表</div>
      <table className="hv-table">
        <thead><tr><th>时间</th><th>策略</th><th>品种</th><th>方向</th><th>数量</th><th>BT价</th><th>真实价</th><th>滑点</th></tr></thead>
        <tbody>
          {MOCK_EXECUTIONS.map(e => {
            const slip = ((e.actual_price - e.backtest_price) / e.backtest_price * 10000).toFixed(1);
            return (
              <tr key={e.fill_id}>
                <td>{e.time}</td><td>{e.strategy}</td><td>{e.instrument}</td>
                <td style={{ color: e.side === 'buy' ? 'var(--success,#3fb950)' : 'var(--destructive)' }}>{e.side}</td>
                <td className="hv-num">{e.qty}</td><td className="hv-num">{e.backtest_price}</td><td className="hv-num">{e.actual_price}</td>
                <td className="hv-num">{slip} bps</td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

// ── P&L Tab ─────────────────────────────────────
export function PnLTab() {
  const data = MOCK_BACKTEST.equity_curve.map(p => ({ date: p.t, equity: p.net }));
  return (
    <div className="hv-tab">
      <div className="hv-section-title">累计 P&L(gross vs net)</div>
      <div className="hv-chart-box">
        <OEquityCurveChart points={data} />
      </div>
      <div className="hv-honest-note">⚠️ Paper 短期 P&L ≠ 策略有效。当前 gate FAIL,这段曲线含运气成分,不代表策略可上 live。</div>

      <div className="hv-section-title">分 regime 分解</div>
      <div className="hv-grid-3">
        {MOCK_BACKTEST.regime_breakdown.map(r => (
          <div key={r.regime} className="hv-metric-card">
            <SafeRegimeBadge regime={r.regime} />
            <span className="hv-metric-val" style={{ color: r.sharpe >= 0 ? 'var(--success,#3fb950)' : 'var(--destructive)' }}>{r.sharpe}</span>
          </div>
        ))}
      </div>
    </div>
  );
}

// ── Audit Tab ───────────────────────────────────
export function AuditTab() {
  const [selected, setSelected] = useState(MOCK_DECISIONS[0]!);
  return (
    <div className="hv-tab">
      <div className="hv-section-title">决策链(GOLD Ed25519 签名)</div>
      <div className="hv-audit-layout">
        <div className="hv-audit-list">
          {MOCK_DECISIONS.map(d => (
            <button key={d.event_id} className="hv-audit-item"
              data-active={selected.event_id === d.event_id ? 'true' : undefined}
              onClick={() => setSelected(d)}>
              <span className="hv-audit-time">{d.time}</span>
              <span className="hv-audit-type">{d.event_type}</span>
              <span className="hv-tier-badge" data-tier={d.conformance_tier}>{d.conformance_tier}</span>
              <span className="hv-sig" style={{ color: d.signature_valid ? 'var(--success,#3fb950)' : 'var(--destructive)' }}>
                {d.signature_valid ? '✓' : '✕'}
              </span>
            </button>
          ))}
        </div>
        <div className="hv-audit-detail">
          <div className="hv-audit-detail__head">
            <span>{selected.event_id}</span>
            <button className="hv-verify-btn">Verify 签名</button>
          </div>
          <pre className="hv-json">{JSON.stringify(selected.decision_payload, null, 2)}</pre>
        </div>
      </div>
    </div>
  );
}
