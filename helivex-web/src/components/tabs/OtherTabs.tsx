/**
 * 其余 Tab:Strategies / Backtest / Executions / P&L / Audit
 * USE_MOCK=false 时从 api-gateway(:8765) 拉真实数据。
 */
'use client';

import { useEffect, useState } from 'react';
import { OGateBadge, ORegimeBadge, OWalkForwardChart, OExecutionFidelity, OEquityCurveChart } from '@helios/blocks';
import {
  MOCK_STRATEGIES, MOCK_BACKTEST, MOCK_EXECUTIONS, MOCK_DECISIONS,
} from '@/lib/mock-data';
import { helivexApi, USE_MOCK, mergeGatewayStrategy } from '@/lib/api-client';
import type { StrategyState, BacktestResult, Execution, AuditDecision } from '@/types/api';

const MOCK_BY_GW_ID: Record<string, StrategyState> = {
  trend_dual:   MOCK_STRATEGIES[0]!,
  vwap_mr_dual: MOCK_STRATEGIES[1]!,
  spot_trend:   MOCK_STRATEGIES[2]!,
};

// ── Strategies Tab ──────────────────────────────────────────────────────────
export function StrategiesTab() {
  const [strategies, setStrategies] = useState<StrategyState[]>(MOCK_STRATEGIES);
  const [expanded, setExpanded] = useState<string | null>('trend_dual');

  useEffect(() => {
    if (USE_MOCK) return;
    helivexApi.strategies()
      .then(rows => setStrategies(rows.map(gw => mergeGatewayStrategy(MOCK_BY_GW_ID[gw.id] ?? MOCK_STRATEGIES[0]!, gw))))
      .catch(console.error);
  }, []);

  return (
    <div className="hv-tab">
      <div className="hv-section-title">策略详情{USE_MOCK ? ' (mock)' : ' (live — gate FAIL 真实)'}</div>
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
              <div className="hv-detail-row"><span>生效配置:</span> {s.indicators.filter(i => i.enabled).map(i => i.name).join(', ')}</div>
              <div className="hv-detail-row"><span>signal:</span> <code>{s.signal_logic.entry}</code></div>
              <div className="hv-detail-row"><span>持仓:</span> {s.position}</div>
              <div className="hv-detail-row"><span>gate:</span> <OGateBadge verdict={s.gate.verdict} dsr={s.gate.dsr} pbo={s.gate.pbo} /></div>
              {s.gate.reason && <div className="hv-detail-row" style={{ color: 'var(--destructive)', fontSize: 11 }}><span>FAIL 原因:</span> {s.gate.reason}</div>}
              <div className="hv-detail-row" style={{ fontSize: 11, color: 'var(--muted-foreground)' }}><span>Paper 信号数:</span> {s.signals_today}</div>
            </div>
          )}
        </div>
      ))}
    </div>
  );
}

// ── Backtest Tab ─────────────────────────────────────────────────────────────
const STRATEGY_OPTIONS = [
  { id: 'trend_dual',   label: '策略1 趋势双向', instruments: ['BTC-USDT-SWAP', 'ETH-USDT-SWAP', 'SOL-USDT-SWAP'] },
  { id: 'vwap_mr_dual', label: '策略2 均值回归', instruments: ['SOL-USDT-SWAP', 'BTC-USDT-SWAP'] },
  { id: 'spot_trend',   label: '策略3 每日趋势', instruments: ['BTC-USDT-SWAP', 'ETH-USDT-SWAP'] },
];

export function BacktestTab() {
  const [stratIdx, setStratIdx] = useState(0);
  const [instIdx, setInstIdx] = useState(0);
  const [bt, setBt] = useState<BacktestResult>(MOCK_BACKTEST);
  const [running, setRunning] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  const runBacktest = async () => {
    if (USE_MOCK) { setBt(MOCK_BACKTEST); return; }
    const strat = STRATEGY_OPTIONS[stratIdx]!;
    const inst  = strat.instruments[instIdx] ?? strat.instruments[0]!;
    setRunning(true); setErr(null);
    try {
      const result = await helivexApi.runBacktest(strat.id, inst);
      setBt(result);
    } catch (e) {
      setErr(String(e));
    } finally {
      setRunning(false);
    }
  };

  return (
    <div className="hv-tab">
      <div className="hv-section-title">Backtest 运行器{USE_MOCK ? ' (mock)' : ' (连真 /backtest/run)'}</div>
      <div className="hv-bt-controls">
        <select className="hv-select" value={stratIdx} onChange={e => { setStratIdx(+e.target.value); setInstIdx(0); }}>
          {STRATEGY_OPTIONS.map((s, i) => <option key={s.id} value={i}>{s.label}</option>)}
        </select>
        <select className="hv-select" value={instIdx} onChange={e => setInstIdx(+e.target.value)}>
          {(STRATEGY_OPTIONS[stratIdx]?.instruments ?? []).map((inst, i) => <option key={inst} value={i}>{inst}</option>)}
        </select>
        <button className="hv-run-gate" onClick={runBacktest} disabled={running}>
          {running ? '运行中…' : 'Run Backtest'}
        </button>
      </div>

      {err && <div className="hv-honest-note" style={{ borderColor: 'var(--destructive)', color: 'var(--destructive)' }}>⚠ {err}</div>}

      <div className="hv-grid-4">
        <div className="hv-metric-card"><span className="hv-metric-label">Sharpe</span><span className="hv-metric-val">{bt.sharpe.toFixed(2)}</span></div>
        <div className="hv-metric-card"><span className="hv-metric-label">Max DD</span><span className="hv-metric-val hv-neg">{(bt.max_drawdown * 100).toFixed(1)}%</span></div>
        <div className="hv-metric-card"><span className="hv-metric-label">信号数</span><span className="hv-metric-val">{bt.fill_count}</span></div>
        <div className="hv-metric-card"><span className="hv-metric-label">年化</span><span className="hv-metric-val">{(bt.annualized * 100).toFixed(1)}%</span></div>
      </div>

      <div className="hv-section-title">Walk-Forward (fold 方差是 FAIL 主因)</div>
      <OWalkForwardChart folds={bt.folds} threshold={0} />

      <div className="hv-section-title">Regime 分段表现 (诚实诊断)</div>
      <div className="hv-grid-3">
        {bt.regime_breakdown.map(r => (
          <div key={r.regime} className="hv-metric-card">
            <ORegimeBadge regime={r.regime} />
            <span className="hv-metric-val" style={{ color: r.sharpe >= 0 ? 'var(--success,#3fb950)' : 'var(--destructive)' }}>
              {r.sharpe >= 0 ? '+' : ''}{r.sharpe.toFixed(2)}
            </span>
          </div>
        ))}
      </div>
      <div className="hv-honest-note">Gate 三策略均 FAIL。此 backtest 结果含样本内过拟合风险，OOS fold 方差过高。</div>
    </div>
  );
}

// ── Executions Tab ───────────────────────────────────────────────────────────
export function ExecutionsTab() {
  const [fills, setFills] = useState<Execution[]>(MOCK_EXECUTIONS);
  const [fidelity, setFidelity] = useState<{ label: string; backtest: number; actual: number; unit?: string; worseWhenHigher: boolean }[]>([
    { label: '平均滑点 (bps)', backtest: 2, actual: 0, worseWhenHigher: true },
    { label: 'fill 数量', backtest: 0, actual: 0, worseWhenHigher: false },
  ]);

  useEffect(() => {
    if (USE_MOCK) return;
    helivexApi.executions().then(({ fills: f, fidelity: fd }) => {
      setFills(f);
      // Build fidelity metrics from gateway fidelity data
      const allFid = fd.reduce((acc, d) => {
        acc.slippage.push(d.mean_slippage_bps ?? 0);
        acc.fills += d.n_fills;
        acc.signals += d.n_signals;
        return acc;
      }, { slippage: [] as number[], fills: 0, signals: 0 });
      const avgSlip = allFid.slippage.length
        ? allFid.slippage.reduce((a, b) => a + b, 0) / allFid.slippage.length
        : 0;
      setFidelity([
        { label: '平均滑点 (bps)', backtest: 2, actual: parseFloat(avgSlip.toFixed(1)), worseWhenHigher: true },
        { label: 'fill / signal', backtest: allFid.signals, actual: allFid.fills, worseWhenHigher: false },
      ]);
    }).catch(console.error);
  }, []);

  return (
    <div className="hv-tab">
      <div className="hv-section-title">执行真实度{USE_MOCK ? ' (mock)' : ' (live)'}</div>
      <OExecutionFidelity metrics={fidelity} />

      <div className="hv-section-title">Fill 列表{fills.length === 0 ? ' — 等待首笔 fill (下一个 bar 收盘)' : ''}</div>
      {fills.length === 0 ? (
        <div className="hv-honest-note">当前无 fill 记录。Paper node 已订阅 OKX Demo 行情，等待 4H/1H/1D bar 收盘后产生信号 → fill。</div>
      ) : (
        <table className="hv-table">
          <thead><tr><th>时间</th><th>策略</th><th>品种</th><th>方向</th><th>数量</th><th>信号价</th><th>真实价</th><th>滑点</th></tr></thead>
          <tbody>
            {fills.map(e => {
              const slip = e.backtest_price > 0
                ? ((e.actual_price - e.backtest_price) / e.backtest_price * 10000).toFixed(1)
                : '—';
              return (
                <tr key={e.fill_id}>
                  <td>{e.time}</td><td style={{ fontSize: 11 }}>{e.strategy}</td><td>{e.instrument}</td>
                  <td style={{ color: e.side === 'buy' ? 'var(--success,#3fb950)' : 'var(--destructive)', fontWeight: 700 }}>{e.side}</td>
                  <td className="hv-num">{e.qty}</td>
                  <td className="hv-num">{e.backtest_price?.toLocaleString()}</td>
                  <td className="hv-num">{e.actual_price?.toLocaleString()}</td>
                  <td className="hv-num">{slip} bps</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      )}
    </div>
  );
}

// ── P&L Tab ──────────────────────────────────────────────────────────────────
export function PnLTab() {
  // undefined = loading, null = loaded but no fills, array = real data
  const [points, setPoints] = useState<{ date: string; equity: number }[] | null | undefined>(
    USE_MOCK ? MOCK_BACKTEST.equity_curve.map(p => ({ date: p.t, equity: p.net })) : undefined
  );
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    if (USE_MOCK) return;
    helivexApi.pnl()
      .then(setPoints)
      .catch(e => setErr(String(e)));
  }, []);

  const hasData = points != null && points.length > 0;

  return (
    <div className="hv-tab">
      <div className="hv-section-title">累计 Paper P&L{USE_MOCK ? ' (mock)' : ''}</div>
      {err && (
        <div className="hv-honest-note" style={{ borderColor: 'var(--destructive)', color: 'var(--destructive)' }}>
          ⚠ API error: {err}
        </div>
      )}
      {!USE_MOCK && points === undefined && (
        <div className="hv-empty">加载中…</div>
      )}
      {hasData ? (
        <div className="hv-chart-box">
          <OEquityCurveChart points={points!} />
        </div>
      ) : (
        !USE_MOCK && points !== undefined && (
          <div className="hv-empty">
            暂无 fill 数据 — 等待首笔 bar 收盘后产生真实 P&amp;L。
          </div>
        )
      )}
      <div className="hv-honest-note">
        {USE_MOCK
          ? '⚠️ 显示 mock backtest 数据。生产环境 USE_MOCK=false。'
          : hasData
            ? '⚠️ Gate FAIL 表示策略存在过拟合风险，P&L 含运气成分，不代表策略可上 live。'
            : '⚠️ 当前 paper.fills 为空，无真实 P&L 数据。Gate 均为 FAIL，纸面交易中，等待 bar 收盘。'}
      </div>
    </div>
  );
}

// ── Audit Tab ────────────────────────────────────────────────────────────────
export function AuditTab() {
  const [decisions, setDecisions] = useState<AuditDecision[]>(MOCK_DECISIONS);
  const [selected, setSelected] = useState<AuditDecision>(MOCK_DECISIONS[0]!);
  const [verifyResult, setVerifyResult] = useState<{ valid: boolean } | null>(null);
  const [verifying, setVerifying] = useState(false);

  useEffect(() => {
    if (USE_MOCK) return;
    helivexApi.decisions().then(rows => {
      if (rows.length) { setDecisions(rows); setSelected(rows[0]!); }
    }).catch(console.error);
  }, []);

  const verify = async () => {
    setVerifying(true); setVerifyResult(null);
    try {
      const r = await helivexApi.verifySig(selected.event_id);
      setVerifyResult(r);
    } catch { setVerifyResult({ valid: false }); }
    finally { setVerifying(false); }
  };

  return (
    <div className="hv-tab">
      <div className="hv-section-title">决策链 (GOLD Ed25519 签名){USE_MOCK ? ' (mock)' : ' (live)'}</div>
      <div className="hv-audit-layout">
        <div className="hv-audit-list">
          {decisions.map(d => (
            <button key={d.event_id} className="hv-audit-item"
              data-active={selected.event_id === d.event_id ? 'true' : undefined}
              onClick={() => { setSelected(d); setVerifyResult(null); }}>
              <span className="hv-audit-time">{d.time}</span>
              <span className="hv-audit-type" style={{ flex: 1, fontSize: 11 }}>{d.event_type}</span>
              <span className="hv-tier-badge" data-tier={d.conformance_tier}>{d.conformance_tier}</span>
              <span style={{ color: d.signature_valid ? 'var(--success,#3fb950)' : 'var(--destructive)', fontSize: 14 }}>
                {d.signature_valid ? '✓' : '✕'}
              </span>
            </button>
          ))}
        </div>
        <div className="hv-audit-detail">
          <div className="hv-audit-detail__head">
            <span style={{ fontSize: 12, fontFamily: 'monospace' }}>{selected.event_id}</span>
            <button className="hv-verify-btn" onClick={verify} disabled={verifying}>
              {verifying ? '验证中…' : 'Verify 签名'}
            </button>
          </div>
          {verifyResult && (
            <div style={{ marginBottom: 8, fontSize: 12, color: verifyResult.valid ? 'var(--success,#3fb950)' : 'var(--destructive)' }}>
              签名验证: {verifyResult.valid ? '✓ 有效 (Ed25519 GOLD)' : '✕ 无效'}
            </div>
          )}
          <pre className="hv-json">{JSON.stringify(selected.decision_payload, null, 2)}</pre>
        </div>
      </div>
      <div className="hv-honest-note">
        GOLD tier：每条 paper 信号在提交前用 Ed25519 私钥签名，公钥可离线验签，防篡改。
        {!USE_MOCK && ' 当前显示真实 paper.signals 记录。'}
      </div>
    </div>
  );
}
