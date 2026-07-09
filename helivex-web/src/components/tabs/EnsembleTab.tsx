/**
 * EnsembleTab — 3O 共识大脑(P2-P6)全链路可视化:
 *   regime(P2)→ 引擎信号(P4,带 promoted 门)→ 共识(P5,只 promoted 驱动)→
 *   共识→风控评估(P6,observe 桥)。全部 observe-only,不接实盘;明示 enforce_mode。
 * 全部真实数据,诚实空状态。
 */
'use client';

import { EmptyState, Skeleton, StaleBanner } from '../EmptyState';
import { ensembleApi } from '@/lib/api-client';
import { useApi } from '@/lib/use-api';
import type { RegimeResp, EnginesResp, ConsensusResp, ConsensusRiskResp } from '@/types/api';

const dirColor = (d: string) =>
  d === 'long' ? 'var(--success, oklch(0.62 0.18 145))' : d === 'short' ? 'var(--destructive)' : 'var(--muted-foreground)';
const regimeColor = (s: string) =>
  s === 'crisis' ? 'var(--destructive)' : s === 'trend' ? 'var(--success, oklch(0.62 0.18 145))' : 'oklch(0.70 0.15 80)';
const sym = (s: string) => s.split('-')[0];

export function EnsembleTab() {
  const { data, loading, error, stale } = useApi(
    () => Promise.all([ensembleApi.regime(), ensembleApi.engines(), ensembleApi.consensus(), ensembleApi.riskEval()]),
    [], 15000, 'ensemble',
  );
  if (loading && !data) return <div className="hv-tab"><Skeleton /></div>;
  if (error && !data) return <div className="hv-tab"><EmptyState text="网关连接失败" sub={error} /></div>;
  const [regime, engines, consensus, risk] = data as [RegimeResp, EnginesResp, ConsensusResp, ConsensusRiskResp];

  // group engine signals by instrument
  const byInst: Record<string, typeof engines.engines> = {};
  for (const e of engines.engines) (byInst[e.instrument] ??= []).push(e);

  return (
    <div className="hv-tab">
      {stale && <StaleBanner error={error!} />}

      {/* 链路总览 honest note */}
      <div className="hv-honest-note">
        3O 共识大脑(helixa→helivex 替换 P2-P6)。链路:regime → 引擎信号(各过门)→ 共识(<strong>只 promoted 引擎驱动</strong>)
        → 全套风控。全部 <strong>observe-only</strong>,不接实盘;当前 enforce_mode = <strong>{risk.enforce_mode}</strong>。
        与 helixa 关键区别:未过门引擎记录但不投票(helixa 什么都投,含 0.2315 准确率的模型)。
      </div>

      {/* Regime(P2)*/}
      <div className="hv-section-title">市场 Regime(P2,advisory)· {regime.as_of ? new Date(regime.as_of).toLocaleString() : '—'}</div>
      {regime.regimes.length === 0 ? <EmptyState text="暂无 regime" sub="helivex-regime-adapter.timer 尚未跑" /> : (
        <div className="hv-grid-3">
          {regime.regimes.map(r => (
            <div key={r.instrument} className="hv-metric-card">
              <span className="hv-metric-label">{sym(r.instrument)}</span>
              <span className="hv-metric-val" style={{ color: regimeColor(r.state) }}>{r.state}</span>
              <div className="hv-micro-row"><span>置信</span><span className="hv-num">{r.confidence != null ? (r.confidence * 100).toFixed(1) + '%' : '—'}</span></div>
              <div className="hv-micro-row"><span>方法</span><span className="hv-num">{r.method_used}</span></div>
            </div>
          ))}
        </div>
      )}

      {/* 引擎信号(P4)*/}
      <div className="hv-section-title">引擎信号(P4,promoted=过自己的门)· {engines.as_of ? new Date(engines.as_of).toLocaleString() : '—'}</div>
      {engines.engines.length === 0 ? <EmptyState text="暂无引擎信号" sub="helivex-signal-engines-adapter.timer 尚未跑" /> : (
        <table className="hv-table" aria-label="引擎信号">
          <thead><tr><th>标的</th><th>引擎</th><th>方向</th><th>分数</th><th>门</th></tr></thead>
          <tbody>
            {engines.engines.map((e, i) => (
              <tr key={i}>
                <td>{sym(e.instrument)}</td>
                <td>{e.engine}</td>
                <td style={{ color: dirColor(e.direction) }}>{e.direction}</td>
                <td className="hv-num">{e.score != null ? e.score.toFixed(3) : '—'}</td>
                <td style={{ color: e.promoted ? 'var(--success,#3fb950)' : 'var(--muted-foreground)' }}>
                  {e.promoted ? '✓ promoted' : '○ observe'}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      {/* 共识(P5)+ 权重 */}
      <div className="hv-section-title">多引擎共识(P5,只 promoted 驱动)· {consensus.as_of ? new Date(consensus.as_of).toLocaleString() : '—'}</div>
      {consensus.consensus.length === 0 ? <EmptyState text="暂无共识" sub="helivex-consensus-adapter.timer 尚未跑" /> : (
        <table className="hv-table" aria-label="共识">
          <thead><tr><th>标的</th><th>方向</th><th>共识分</th><th>Kelly</th><th>一致度</th><th>regime</th><th>情绪偏置</th><th>可执行</th><th>#promoted</th></tr></thead>
          <tbody>
            {consensus.consensus.map(c => (
              <tr key={c.instrument}>
                <td>{sym(c.instrument)}</td>
                <td style={{ color: dirColor(c.final_direction) }}>{c.final_direction}</td>
                <td className="hv-num">{c.consensus_score != null ? c.consensus_score.toFixed(3) : '—'}</td>
                <td className="hv-num">{c.kelly_position != null ? (c.kelly_position * 100).toFixed(1) + '%' : '—'}</td>
                <td className="hv-num">{c.agreement_ratio != null ? (c.agreement_ratio * 100).toFixed(0) + '%' : '—'}</td>
                <td style={{ color: regimeColor(c.regime_state) }}>{c.regime_state}</td>
                <td className="hv-num">{c.sentiment_bias != null ? c.sentiment_bias.toFixed(2) : '—'}</td>
                <td style={{ color: c.should_execute ? 'var(--success,#3fb950)' : 'var(--muted-foreground)' }}>{c.should_execute ? '是' : '否'}</td>
                <td className="hv-num">{c.n_promoted}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {consensus.weights.length > 0 && (
        <div className="hv-grid-3">
          {consensus.weights.map(w => (
            <div key={w.engine} className="hv-metric-card">
              <span className="hv-metric-label">{w.engine} 权重(EWMA)</span>
              <span className="hv-metric-val">{w.dyn_weight.toFixed(3)}</span>
              <div className="hv-micro-row"><span>base / 准确率</span><span className="hv-num">{w.base_weight.toFixed(1)} / {w.accuracy != null ? (w.accuracy * 100).toFixed(0) + '%' : '—'}</span></div>
            </div>
          ))}
        </div>
      )}

      {/* 共识→风控评估(P6,observe)*/}
      <div className="hv-section-title">共识→全套风控评估(P6,observe 桥)· {risk.as_of ? new Date(risk.as_of).toLocaleString() : '—'}</div>
      {risk.evals.length === 0 ? <EmptyState text="暂无风控评估" sub="helivex-consensus-risk-adapter.timer 尚未跑" /> : (
        <table className="hv-table" aria-label="共识风控评估">
          <thead><tr><th>标的</th><th>方向</th><th>共识可执行</th><th>过风控</th><th>仓位(USD)</th><th>拦截档</th><th>crisis 收缩</th></tr></thead>
          <tbody>
            {risk.evals.map(e => (
              <tr key={e.instrument}>
                <td>{sym(e.instrument)}</td>
                <td style={{ color: dirColor(e.direction) }}>{e.direction}</td>
                <td>{e.should_execute ? '是' : '否'}</td>
                <td style={{ color: e.approved ? 'var(--success,#3fb950)' : 'var(--muted-foreground)' }}>{e.approved ? '✓' : '✕'}</td>
                <td className="hv-num">{e.final_notional != null ? '$' + e.final_notional.toFixed(0) : '—'}</td>
                <td>{e.blocking_stage ?? '—'}</td>
                <td>{e.crisis_scaled ? '×0.1' : '—'}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      <div className="hv-honest-note">
        风控评估为 observe-only:算出"共识若执行会不会过全套风控(crisis→三层裁剪→fee/edge)、多大仓位",
        <strong>不下单</strong>。接 enforce(真下 paper 单)是后续独立、需人工放行的一步。
      </div>
    </div>
  );
}
