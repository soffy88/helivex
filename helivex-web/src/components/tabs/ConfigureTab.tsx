/**
 * ConfigureTab — 配置编辑器(Wiki 核心诉求,P1)
 * 6 指标卡 + signal_logic + 实时 gate 反馈 + mode 切换(带 gate 保护)
 * Run Gate 连真实 /gate/run endpoint。
 */
'use client';

import { useEffect, useState } from 'react';
import { OIndicatorCard, OGateBadge } from '@helios/blocks';
import { MOCK_STRATEGIES, MOCK_GATE_RESULT } from '@/lib/mock-data';
import type { GateResult, StrategyState } from '@/types/api';
import { helivexApi, USE_MOCK, mergeGatewayStrategy } from '@/lib/api-client';

const MOCK_BY_GW_ID: Record<string, StrategyState> = {
  trend_dual:   MOCK_STRATEGIES[0]!,
  vwap_mr_dual: MOCK_STRATEGIES[1]!,
  spot_trend:   MOCK_STRATEGIES[2]!,
};

const GW_IDS = ['trend_dual', 'vwap_mr_dual', 'spot_trend'];

export function ConfigureTab() {
  const [strategies, setStrategies] = useState<StrategyState[]>(MOCK_STRATEGIES);
  const [stratIdx, setStratIdx] = useState(0);
  const [strategy, setStrategy] = useState<StrategyState>(MOCK_STRATEGIES[0]!);
  const [gateResult, setGateResult] = useState<GateResult | null>(null);
  const [running, setRunning] = useState(false);

  useEffect(() => {
    if (USE_MOCK) return;
    helivexApi.strategies()
      .then(rows => {
        const merged = rows.map(gw => mergeGatewayStrategy(MOCK_BY_GW_ID[gw.id] ?? MOCK_STRATEGIES[0]!, gw));
        setStrategies(merged);
        setStrategy(merged[stratIdx] ?? merged[0]!);
      })
      .catch(console.error);
  }, []);

  const switchStrat = (i: number) => {
    setStratIdx(i);
    setStrategy(strategies[i] ?? MOCK_STRATEGIES[i] ?? MOCK_STRATEGIES[0]!);
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
    if (USE_MOCK) {
      setTimeout(() => { setGateResult(MOCK_GATE_RESULT); setRunning(false); }, 1200);
      return;
    }
    try {
      // Call /gate/run?config=<strategy_id> — uses YAML config on disk, runs real backtest gate
      const gwId = GW_IDS[stratIdx] ?? GW_IDS[0]!;
      const r = await helivexApi.runGate(gwId);
      setGateResult(r);
    } catch (e) {
      setGateResult({ ...MOCK_GATE_RESULT, verdict: 'fail', reason: String(e) });
    } finally {
      setRunning(false);
    }
  };

  return (
    <div className="hv-tab">
      {/* 策略选择 */}
      <div className="hv-strat-tabs">
        {strategies.map((s, i) => (
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
      <div className="hv-section-title">指标配置 (6){USE_MOCK ? ' (mock params)' : ''}</div>
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
          {running ? '运行 gate 中…' : USE_MOCK ? 'Run Gate (mock)' : 'Run Gate (真实)'}
        </button>
        {gateResult && (
          <div className="hv-gate-result">
            <OGateBadge verdict={gateResult.verdict} dsr={gateResult.dsr} pbo={gateResult.pbo} reason={gateResult.reason} />
            <div className="hv-gate-metrics">
              <span>Gross SR <strong>{typeof gateResult.gross_sr === 'number' ? gateResult.gross_sr.toFixed(3) : gateResult.gross_sr}</strong></span>
              <span>OOS Sharpe <strong>{typeof gateResult.oos_sharpe === 'number' ? gateResult.oos_sharpe.toFixed(3) : gateResult.oos_sharpe}</strong></span>
              <span>DSR <strong>{typeof gateResult.dsr === 'number' ? gateResult.dsr.toFixed(3) : gateResult.dsr}</strong></span>
              <span>PBO <strong>{typeof gateResult.pbo === 'number' ? (gateResult.pbo * 100).toFixed(0) : gateResult.pbo}%</strong></span>
            </div>
            {gateResult.reason && <div className="hv-gate-reason">{gateResult.reason}</div>}
            {/* 防 p-hacking 提醒 — 诚实展示全局 trial 计数 */}
            <div className="hv-trial-warn">
              ⚠️ 已试 <strong>{gateResult.global_trial_count}</strong> 个配置，DSR 阈值 = <strong>{typeof gateResult.dsr_threshold === 'number' ? gateResult.dsr_threshold.toFixed(3) : gateResult.dsr_threshold}</strong>。
              试太多配置挑最好 = p-hacking，全局 N 校正已提高门槛，DSR 需更高才能 PASS。
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
