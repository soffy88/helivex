/**
 * ConfigureTab — 策略配置查看 + 实时 gate(真实数据,无 mock)
 * 注:配置「编辑→PUT」是后续轮次(后端 /config 是嵌套对象,编辑器需单独做);
 *     本轮去 mock:展示真实配置 + 真实 Run Gate。
 */
'use client';

import { useState } from 'react';
import { SafeGateBadge } from '../SafeBadges';
import { EmptyState } from '../EmptyState';
import { helivexApi } from '@/lib/api-client';
import { useApi } from '@/lib/use-api';
import type { StrategyState, GateResult } from '@/types/api';

interface RawConfig {
  description?: string; timeframe?: string; instruments?: string[];
  indicators?: Record<string, Record<string, unknown>>;
  signal_logic?: Record<string, unknown>; risk?: Record<string, unknown>;
  gate?: Record<string, unknown>; gate_status?: string; gate_reason?: string;
}

export function ConfigureTab() {
  const { data: strategies, loading, error } = useApi<StrategyState[]>(() => helivexApi.strategies(), []);
  const [sel, setSel] = useState<string | null>(null);
  const id = sel ?? strategies?.[0]?.strategy_id ?? null;
  const cfg = useApi<RawConfig>(() => helivexApi.getConfig(id!) as unknown as Promise<RawConfig>, [id]);
  const [gateResult, setGateResult] = useState<GateResult | null>(null);
  const [running, setRunning] = useState(false);
  const [gateErr, setGateErr] = useState<string | null>(null);

  if (loading) return <div className="hv-tab"><EmptyState text="加载中…" /></div>;
  if (error) return <div className="hv-tab"><EmptyState text="网关连接失败" sub={error} /></div>;
  const list = strategies ?? [];
  if (list.length === 0) return <div className="hv-tab"><EmptyState text="暂无策略" /></div>;
  const c = cfg.data;

  const runGate = async () => {
    if (!id) return;
    setRunning(true); setGateErr(null);
    try { setGateResult(await helivexApi.runGate(id)); }
    catch (e) { setGateErr(String((e as Error)?.message ?? e)); }
    finally { setRunning(false); }
  };

  return (
    <div className="hv-tab">
      <div className="hv-strat-tabs">
        {list.map(s => (
          <button key={s.strategy_id} className="hv-strat-tab"
            data-active={(id === s.strategy_id) ? 'true' : undefined}
            onClick={() => { setSel(s.strategy_id); setGateResult(null); setGateErr(null); }}>{s.name}</button>
        ))}
      </div>

      {cfg.loading ? <EmptyState text="加载配置…" /> : cfg.error ? <EmptyState text="配置加载失败" sub={cfg.error} /> : c && (
        <>
          <div className="hv-honest-note">{c.description ?? ''}（{c.timeframe ?? ''} · {(c.instruments ?? []).join(', ')}）</div>

          <div className="hv-section-title">Signal Logic</div>
          <pre className="hv-json">{JSON.stringify(c.signal_logic ?? {}, null, 2)}</pre>

          <div className="hv-section-title">指标配置(真实)</div>
          {!c.indicators || Object.keys(c.indicators).length === 0 ? (
            <EmptyState text="该策略无可调指标" />
          ) : (
            <div className="hv-grid-indicators">
              {Object.entries(c.indicators).map(([name, params]) => (
                <div key={name} className="hv-metric-card" style={{ alignItems: 'flex-start' }}>
                  <span className="hv-strat-name">{name} {(params as { enabled?: boolean }).enabled ? '✓' : '✕'}</span>
                  <div className="hv-detail-row" style={{ fontSize: 'var(--text-sm)' }}>
                    {Object.entries(params).filter(([k]) => k !== 'enabled').map(([k, v]) => `${k}=${v}`).join('  ')}
                  </div>
                </div>
              ))}
            </div>
          )}
          <div className="hv-honest-note">⚠️ 配置编辑/保存为后续轮次;当前为只读真实配置。</div>
        </>
      )}

      <div className="hv-gate-section">
        <button className="hv-run-gate" onClick={runGate} disabled={running}>{running ? '运行 gate 中…' : 'Run Gate'}</button>
        {gateErr && <div className="hv-gate-reason" style={{ color: 'var(--destructive)' }}>gate 运行失败:{gateErr}</div>}
        {gateResult && (
          <div className="hv-gate-result">
            <SafeGateBadge verdict={gateResult.verdict} dsr={gateResult.dsr} pbo={gateResult.pbo} reason={gateResult.reason} />
            <div className="hv-gate-metrics">
              <span>Gross SR <strong>{gateResult.gross_sr}</strong></span>
              <span>OOS Sharpe <strong>{gateResult.oos_sharpe}</strong></span>
              <span>DSR <strong>{gateResult.dsr}</strong></span>
              <span>PBO <strong>{(gateResult.pbo * 100).toFixed(0)}%</strong></span>
            </div>
            {gateResult.reason && <div className="hv-gate-reason">{gateResult.reason}</div>}
            {gateResult.global_trial_count != null && (
              <div className="hv-trial-warn">⚠️ 已试 {gateResult.global_trial_count} 个配置,DSR 阈值 = {gateResult.dsr_threshold}。多试挑最好 = p-hacking,全局 N 校正已提高门槛。</div>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
