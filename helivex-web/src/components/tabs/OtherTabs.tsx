/**
 * 其余 Tab:Strategies / Backtest / Executions / P&L / Audit
 */
'use client';

import { useState, useEffect } from 'react';
import { OGateBadge, ORegimeBadge, OWalkForwardChart, OExecutionFidelity, OEquityCurveChart } from '@helios/blocks';
import {
  MOCK_BACKTEST, MOCK_EXECUTIONS,
} from '@/lib/mock-data';
import { helivexApi } from '@/lib/api-client';
import type { StrategyState } from '@/types/api';

// ── Strategies Tab ──────────────────────────────
export function StrategiesTab({ strategies, onDrill }: { strategies: StrategyState[]; onDrill?: (id: string) => void }) {
  const [expanded, setExpanded] = useState<string | null>(strategies[0]?.strategy_id ?? null);
  return (
    <div className="hv-tab">
      {strategies.map(s => (
        <div key={s.strategy_id} className="hv-strat-detail">
          <button className="hv-strat-detail__head" onClick={() => setExpanded(e => e === s.strategy_id ? null : s.strategy_id)}>
            <span className="hv-strat-name">{s.name}</span>
            <ORegimeBadge regime={s.regime} compact />
            <OGateBadge verdict={s.gate.verdict} dsr={s.gate.dsr} pbo={s.gate.pbo} compact />
            <span className="hv-mode-badge">{s.mode}</span>
            <span className="hv-expand-icon">{expanded === s.strategy_id ? '▾' : '▸'}</span>
          </button>
          {expanded === s.strategy_id && (
            <div className="hv-strat-detail__body">
              <div className="hv-detail-row"><span>信号数:</span> {s.signals_today}</div>
              <div className="hv-detail-row"><span>gate:</span> <OGateBadge verdict={s.gate.verdict} dsr={s.gate.dsr} pbo={s.gate.pbo} /></div>
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
            <ORegimeBadge regime={r.regime} />
            <span className="hv-metric-val" style={{ color: r.sharpe >= 0 ? 'var(--success,#3fb950)' : 'var(--destructive)' }}>
              {r.sharpe >= 0 ? '+' : ''}{r.sharpe}
            </span>
          </div>
        ))}
      </div>
    </div>
  );
}

// ── Executions Tab ──────────────────────────────
export function ExecutionsTab() {
  const fidelity = [
    { label: '平均滑点 (bps)', backtest: 2, actual: 0, worseWhenHigher: true },
    { label: 'maker fill rate', backtest: 95, actual: 0, unit: '%', worseWhenHigher: false },
  ];
  return (
    <div className="hv-tab">
      <div className="hv-section-title">执行真实度(等首笔 fill)</div>
      <OExecutionFidelity metrics={fidelity} />
      <div className="hv-honest-note">暂无成交记录。等首笔 fill 后此视图将显示真实滑点数据。</div>
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
    </div>
  );
}

// ── Audit Tab — 切真 ────────────────────────────
interface AuditRow {
  id: number; ts: string; strategy_id: string; instrument: string;
  action: string; signal_price: number; audit_record_id: string;
  has_signature: boolean; tier: string;
}

export function AuditTab() {
  const [decisions, setDecisions] = useState<AuditRow[]>([]);
  const [selected, setSelected] = useState<AuditRow | null>(null);
  const [chain, setChain] = useState<any>(null);

  useEffect(() => {
    helivexApi.decisions().then((rows: any) => {
      const arr: AuditRow[] = Array.isArray(rows) ? rows : [];
      setDecisions(arr);
      if (arr.length) setSelected(arr[0]!);
    }).catch(console.error);
    helivexApi.chainHealth().then(setChain).catch(console.error);
  }, []);

  return (
    <div className="hv-tab">
      {chain && (
        <div className="hv-chain-banner" data-intact={(chain as any).ok ? 'true' : undefined}>
          <span className="hv-chain-icon">{(chain as any).ok ? '✓' : '✕'}</span>
          <span>审计链 {(chain as any).ok ? '完整' : '断裂'} · {(chain as any).n_total} records · {(chain as any).n_valid} valid GOLD</span>
        </div>
      )}
      <div className="hv-section-title">决策链(GOLD Ed25519 签名)</div>
      <div className="hv-audit-layout">
        <div className="hv-audit-list">
          {decisions.map(d => (
            <button key={d.id} className="hv-audit-item"
              data-active={selected?.id === d.id ? 'true' : undefined}
              onClick={() => setSelected(d)}>
              <span className="hv-audit-time">{d.ts.replace('T', ' ').slice(0, 19)}</span>
              <span className="hv-audit-type">{d.action}</span>
              <span className="hv-tier-badge" data-tier={d.tier}>{d.tier}</span>
              <span className="hv-sig" style={{ color: d.has_signature ? 'var(--success,#3fb950)' : 'var(--muted-foreground)' }}>
                {d.has_signature ? '✓' : '—'}
              </span>
            </button>
          ))}
          {decisions.length === 0 && <div className="hv-honest-note">加载中…</div>}
        </div>
        <div className="hv-audit-detail">
          {selected && (
            <>
              <div className="hv-audit-detail__head">
                <span>{selected.audit_record_id}</span>
              </div>
              <pre className="hv-json">{JSON.stringify({
                id: selected.id,
                strategy: selected.strategy_id,
                instrument: selected.instrument,
                action: selected.action,
                price: selected.signal_price,
                tier: selected.tier,
              }, null, 2)}</pre>
            </>
          )}
        </div>
      </div>
    </div>
  );
}
