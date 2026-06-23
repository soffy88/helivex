/**
 * 其余 Tab:Strategies / Backtest / Executions / P&L / Audit
 */
'use client';

import { useState, useEffect } from 'react';
import { OWalkForwardChart, OEquityCurveChart } from '@helios/blocks';
import { SafeGateBadge, SafeRegimeBadge } from '../SafeBadges';
import { EmptyState } from '../EmptyState';
import { helivexApi } from '@/lib/api-client';
import type { ExecutionsResponse } from '@/types/api';
import {
  MOCK_STRATEGIES, MOCK_BACKTEST, MOCK_DECISIONS,
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

// ── Executions Tab (真实 /executions,无 mock,无成交即诚实空状态)──────────
const fmt = (v: number | null, d = 1, suffix = '') =>
  v === null || v === undefined ? '—' : `${v.toFixed(d)}${suffix}`;

export function ExecutionsTab() {
  const [data, setData] = useState<ExecutionsResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let alive = true;
    const load = () => {
      helivexApi.executions()
        .then(d => { if (alive) { setData(d); setError(null); } })
        .catch(e => { if (alive) setError(String(e?.message ?? e)); })
        .finally(() => { if (alive) setLoading(false); });
    };
    load();
    const t = setInterval(load, 15000);   // refresh — first fill arrives async
    return () => { alive = false; clearInterval(t); };
  }, []);

  if (loading) return <div className="hv-tab"><EmptyState text="加载中…" /></div>;
  if (error) return <div className="hv-tab"><EmptyState text="网关连接失败" sub={`/executions: ${error}`} /></div>;

  const fidelity = data?.fidelity ?? [];
  const fills = data?.fills ?? [];
  const sideColor = (s: string) =>
    s.toUpperCase() === 'BUY' ? 'var(--success,#3fb950)' : 'var(--destructive)';

  return (
    <div className="hv-tab">
      <div className="hv-section-title">执行真实度(真实成交 vs backtest 假设,决定 backtest 可信度)</div>
      {fidelity.length === 0 ? (
        <EmptyState
          text="尚无真实成交 — 无执行真实度可算"
          sub="backtest 假设(scalp ~2bps / 其余 ~10bps)需首笔真实 fill 才能校验。绝不用假数据冒充。"
        />
      ) : (
        <table className="hv-table">
          <thead><tr>
            <th>策略</th><th>signals</th><th>fills</th><th>fill rate</th>
            <th>平均滑点</th><th>p95 滑点</th><th>平均延迟</th>
          </tr></thead>
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
          <thead><tr>
            <th>时间</th><th>策略</th><th>品种</th><th>方向</th><th>数量</th>
            <th>信号价</th><th>真实价</th><th>滑点</th><th>类型</th><th>延迟</th>
          </tr></thead>
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
