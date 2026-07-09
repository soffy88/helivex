/**
 * HomeTab — 落地页(helixa 首页版式 + 原 Overview 策略钻取,IA 重构后二合一)。
 * 定位「运营视图」:组合聚合 KPI · 策略切换 · 状态卡 · 每策略资金/持仓/统计/执行/信号/成交 ·
 * 市场情绪&共识【摘要卡】· 事件流。
 * 去重原则:引擎投票全表 / K线 / 决策轨迹 / 共识全表 归 Ensemble;这里只放摘要 + 跳转。
 * 全部真实数据,observe-only。显示 helivex 的 5 个策略(helixa 策略族的独立重实现)。
 */
'use client';

import { useState } from 'react';
import { EmptyState, Skeleton, StaleBanner } from '../EmptyState';
import { helivexApi, portfolioApi, riskApi, ensembleApi, streamApi } from '@/lib/api-client';
import { useApi } from '@/lib/use-api';
import { SafeGateBadge } from '../SafeBadges';
import { EquityView, PositionsView, StatsView, ExecutionView, SignalsView, TradesView } from '../StrategyViews';
import type {
  StrategyState, PaperAccount, PortfolioSummary, RiskStatus, FgiResp,
  RegimeResp, ConsensusResp, TimelineResp,
} from '@/types/api';

// helivex 策略 → helixa 风格短标签
const STRAT_TAG: Record<string, string> = {
  spot_trend: '现货 SPOT', scalp_5m: '日内 SCALP', trend_dual: '趋势 TREND',
  vwap_mr_dual: '均值回归 MR', ler_okx: '研究 LER',
};
const tag = (id: string) => STRAT_TAG[id] ?? id;
const sym = (s: string) => s.split('-')[0].split('.')[0];
const pnlColor = (v: number) => (v > 0 ? 'var(--success,#3fb950)' : v < 0 ? 'var(--destructive)' : 'var(--muted-foreground)');
const dirColor = (d: string) =>
  d === 'long' ? 'var(--success,#3fb950)' : d === 'short' ? 'var(--destructive)' : 'var(--muted-foreground)';
const regimeColor = (s: string) =>
  s === 'crisis' ? 'var(--destructive)' : s === 'trend' ? 'var(--success,#3fb950)' : 'oklch(0.70 0.15 80)';

export function HomeTab() {
  const { data, loading, error, stale } = useApi(
    () => Promise.all([
      helivexApi.strategies(), helivexApi.account(), portfolioApi.summary(), riskApi.status(),
      streamApi.fgi(), ensembleApi.regime(), ensembleApi.consensus(),
    ]),
    [], 15000, 'home',
  );
  const tl = useApi<TimelineResp>(() => streamApi.timeline(20), [], 15000, 'home-tl');
  const [sel, setSel] = useState<string | null>(null);

  if (loading && !data) return <div className="hv-tab"><Skeleton /></div>;
  if (error && !data) return <div className="hv-tab"><EmptyState text="网关连接失败" sub={error} /></div>;
  const [strategies, account, summary, risk, fgi, regime, consensus] =
    data as [StrategyState[], PaperAccount, PortfolioSummary, RiskStatus, FgiResp, RegimeResp, ConsensusResp];

  const id = sel ?? strategies[0]?.strategy_id ?? null;
  const cur = strategies.find(s => s.strategy_id === id) ?? null;
  const tripped = risk.kill_switch.tripped;
  // 每标的共识方向速查(摘要用)
  const consByInst: Record<string, ConsensusResp['consensus'][number]> = {};
  for (const c of consensus.consensus) consByInst[c.instrument] = c;

  return (
    <div className="hv-tab">
      {stale && <StaleBanner error={error!} />}

      {/* ── 组合聚合 KPI 条 ── */}
      <div className="hv-grid-3">
        <div className="hv-metric-card"><span className="hv-metric-label">总净值 (NAV)</span>
          <span className="hv-metric-value">${risk.nav.toFixed(2)}</span></div>
        <div className="hv-metric-card"><span className="hv-metric-label">今日净盈亏</span>
          <span className="hv-metric-value" style={{ color: pnlColor(account.pnl_today_net) }}>${account.pnl_today_net.toFixed(2)}</span></div>
        <div className="hv-metric-card"><span className="hv-metric-label">回撤 (峰值 ${risk.peak.toFixed(0)})</span>
          <span className="hv-metric-value" style={{ color: risk.drawdown_pct > risk.caps.max_drawdown_pct * 0.66 ? 'var(--destructive)' : 'var(--muted-foreground)' }}>{risk.drawdown_pct.toFixed(2)}%</span></div>
        <div className="hv-metric-card"><span className="hv-metric-label">总持仓</span>
          <span className="hv-metric-value">{summary.total_positions}</span></div>
        <div className="hv-metric-card"><span className="hv-metric-label">未实现 / 已实现</span>
          <span className="hv-metric-value" style={{ fontSize: 'var(--text-sm)' }}>
            <span style={{ color: pnlColor(summary.total_unrealized_pnl) }}>${summary.total_unrealized_pnl.toFixed(2)}</span>
            {' / '}<span style={{ color: pnlColor(summary.total_realized_pnl) }}>${summary.total_realized_pnl.toFixed(2)}</span>
          </span></div>
        <div className="hv-metric-card"><span className="hv-metric-label">熔断</span>
          <span className="hv-metric-value" style={{ color: tripped ? 'var(--destructive)' : 'var(--success,#3fb950)' }}>{tripped ? '🛑 已熔断' : '✅ 正常'}</span></div>
      </div>

      {/* ── 策略切换 ── */}
      <div className="hv-strat-tabs" style={{ marginTop: 8 }}>
        {strategies.map(s => (
          <button key={s.strategy_id} className="hv-strat-tab"
            data-active={(id === s.strategy_id) ? 'true' : undefined}
            onClick={() => setSel(s.strategy_id)}>{tag(s.strategy_id)}</button>
        ))}
      </div>

      {/* ── 状态卡 ── */}
      {cur && (
        <div className="hv-honest-note" style={{ display: 'flex', gap: 12, alignItems: 'center', flexWrap: 'wrap' }}>
          <strong>{cur.name}</strong>
          <SafeGateBadge verdict={cur.gate.verdict} dsr={cur.gate.dsr} pbo={cur.gate.pbo} />
          <span>{cur.position && cur.position !== '—' && cur.position !== 'flat'
            ? <span style={{ color: 'var(--success,#3fb950)' }}>持仓中 · {cur.position}</span>
            : <span style={{ color: 'var(--muted-foreground)' }}>观望(无持仓)</span>}</span>
          <span>今日信号 {cur.signals_today}</span>
          <span style={{ color: 'var(--muted-foreground)' }}>regime {cur.regime}</span>
        </div>
      )}

      {/* ── 每策略钻取:资金/持仓 · 统计/执行 · 信号/成交 ── */}
      {id && (
        <>
          <div className="hv-grid-2">
            <div className="hv-flat-col"><div className="hv-section-title">资金曲线 — {cur?.name}</div><EquityView id={id} /></div>
            <div className="hv-flat-col"><div className="hv-section-title">持仓</div><PositionsView id={id} /></div>
          </div>
          <div className="hv-grid-2">
            <div className="hv-flat-col"><div className="hv-section-title">统计</div><StatsView id={id} /></div>
            <div className="hv-flat-col"><div className="hv-section-title">执行质量(真实滑点/延迟)</div><ExecutionView id={id} /></div>
          </div>
          <div className="hv-grid-2">
            <div className="hv-flat-col"><div className="hv-section-title">信号历史</div><SignalsView id={id} /></div>
            <div className="hv-flat-col"><div className="hv-section-title">成交历史</div><TradesView id={id} /></div>
          </div>
        </>
      )}

      {/* ── 市场情绪 & 共识【摘要】· 全表见 Ensemble ── */}
      <div className="hv-section-title">市场情绪 & 共识摘要 <span style={{ color: 'var(--muted-foreground)', fontWeight: 400, fontSize: 'var(--text-xs)' }}>· 引擎投票/K线/决策轨迹详见 Ensemble</span></div>
      <div className="hv-grid-3">
        {fgi.value != null && (
          <div className="hv-metric-card">
            <span className="hv-metric-label">Fear & Greed · 逆向偏置</span>
            <span className="hv-metric-value" style={{ color: pnlColor(fgi.contrarian_bias) }}>
              {fgi.value.toFixed(0)} {fgi.classification} · {fgi.contrarian_bias > 0 ? '+' : ''}{fgi.contrarian_bias.toFixed(2)}
            </span>
          </div>
        )}
        {regime.regimes.map(r => {
          const c = consByInst[r.instrument];
          return (
            <div key={r.instrument} className="hv-metric-card">
              <span className="hv-metric-label">{sym(r.instrument)}</span>
              <span className="hv-metric-value" style={{ fontSize: 'var(--text-sm)' }}>
                <span style={{ color: regimeColor(r.state) }}>{r.state}</span>
                {c && <> · <span style={{ color: dirColor(c.final_direction) }}>{c.final_direction}</span>{c.should_execute ? ' ✅' : ' 观察'}</>}
              </span>
            </div>
          );
        })}
      </div>

      {/* ── 事件流 ── */}
      <div className="hv-section-title">事件流(成交 · 风控 · 共识)</div>
      {(tl.data?.events ?? []).length === 0 ? <EmptyState text="暂无事件" /> : (
        <table className="hv-table" aria-label="事件流">
          <thead><tr><th>时间</th><th>类型</th><th>事件</th></tr></thead>
          <tbody>
            {tl.data!.events.slice(0, 12).map((e, i) => (
              <tr key={i}>
                <td className="hv-num">{new Date(e.ts).toLocaleTimeString()}</td>
                <td style={{ color: e.category.startsWith('risk') ? 'var(--destructive)' : e.category === 'fill' ? 'var(--muted-foreground)' : 'oklch(0.70 0.12 240)' }}>{e.category}</td>
                <td>{e.label}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      <div className="hv-honest-note">
        落地页(helixa 首页版式 + 策略钻取合一)。显示 helivex 的 5 个策略(helixa 策略族的独立
        重实现,非逐字移植)。共识大脑全明细(引擎投票/K线/决策轨迹/权重/风控评估)见
        <strong> Ensemble</strong>;组合曲线/相关性/归因见 <strong>Portfolio</strong>。全部 observe-only。
      </div>
    </div>
  );
}
