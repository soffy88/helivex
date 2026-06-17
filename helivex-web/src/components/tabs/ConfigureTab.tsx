/**
 * ConfigureTab — 配置编辑器(Wiki 核心诉求,P1)
 * 6 指标卡 + signal_logic + 实时 gate 反馈 + mode 切换(带 gate 保护)
 */
'use client';

import { useState } from 'react';
import { OIndicatorCard } from '@helios/blocks';
import { SafeGateBadge } from '../SafeBadges';
import { MOCK_STRATEGIES, MOCK_GATE_RESULT } from '@/lib/mock-data';
import type { GateResult } from '@/types/api';
import { helivexApi, USE_MOCK } from '@/lib/api-client';

export function ConfigureTab() {
  const [stratIdx, setStratIdx] = useState(0);
  const [strategy, setStrategy] = useState(MOCK_STRATEGIES[0]!);
  const [gateResult, setGateResult] = useState<GateResult | null>(null);
  const [running, setRunning] = useState(false);

  const switchStrat = (i: number) => {
    setStratIdx(i);
    setStrategy(MOCK_STRATEGIES[i]!);
    setGateResult(null);
  };

  const toggleIndicator = (name: string, enabled: boolean) => {
    setStrategy(s => ({ ...s, indicators: s.indicators.map(ind => ind.name === name ? { ...ind, enabled } : ind) }));
  };
  const changeParam = (name: string, key: string, value: number) => {
    setStrategy(s => ({ ...s, indicators: s.indicators.map(ind =>
      ind.name === name ? { ...ind, params: ind.params.map(p => p.key === key ? { ...p, value } : p) } : ind) }));
  };

  const runGate = async () => {
    setRunning(true);
    if (USE_MOCK) { setTimeout(() => { setGateResult(MOCK_GATE_RESULT); setRunning(false); }, 1200); return; }
    try { const r = await helivexApi.runGate(strategy.strategy_id, strategy.indicators); setGateResult(r); }
    finally { setRunning(false); }
  };

  return (
    <div className="hv-tab">
      {/* 策略选择 */}
      <div className="hv-strat-tabs">
        {MOCK_STRATEGIES.map((s, i) => (
          <button key={s.strategy_id} className="hv-strat-tab"
            data-active={stratIdx === i ? 'true' : undefined}
            onClick={() => switchStrat(i)}>{s.name}</button>
        ))}
      </div>

      {/* signal_logic */}
      <div className="hv-signal-logic">
        <div className="hv-section-title">Signal Logic</div>
        <div className="hv-logic-row"><span className="hv-logic-label">entry</span><code>{strategy.signal_logic.entry}</code></div>
        <div className="hv-logic-row"><span className="hv-logic-label">exit</span><code>{strategy.signal_logic.exit}</code></div>
        <div className="hv-logic-controls">
          <label className="hv-logic-ctrl">
            min_confluence
            <input type="number" min={1} max={6} value={strategy.signal_logic.min_confluence}
              onChange={e => setStrategy(s => ({ ...s, signal_logic: { ...s.signal_logic, min_confluence: Number(e.target.value) } }))} />
          </label>
          <label className="hv-logic-ctrl">
            direction_mode
            <select value={strategy.signal_logic.direction_mode}
              onChange={e => setStrategy(s => ({ ...s, signal_logic: { ...s.signal_logic, direction_mode: e.target.value as 'dual' } }))}>
              <option value="dual">dual</option><option value="long_only">long_only</option><option value="short_only">short_only</option>
            </select>
          </label>
        </div>
      </div>

      {/* 6 指标卡 */}
      <div className="hv-section-title">指标配置(6)</div>
      <div className="hv-grid-indicators">
        {strategy.indicators.map(ind => (
          <OIndicatorCard
            key={ind.name}
            name={ind.name} enabled={ind.enabled} role={ind.role} params={ind.params}
            onToggle={(e) => toggleIndicator(ind.name, e)}
            onParamChange={(k, v) => changeParam(ind.name, k, v)}
          />
        ))}
      </div>

      {/* 实时 gate */}
      <div className="hv-gate-section">
        <button className="hv-run-gate" onClick={runGate} disabled={running}>
          {running ? '运行 gate 中…' : 'Run Gate'}
        </button>
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
            {/* 防 p-hacking 提醒 */}
            <div className="hv-trial-warn">
              ⚠️ 已试 {gateResult.global_trial_count} 个配置,DSR 阈值 = {gateResult.dsr_threshold}。
              试太多配置挑最好 = p-hacking,全局 N 校正已提高门槛。
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
