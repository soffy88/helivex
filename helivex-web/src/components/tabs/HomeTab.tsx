/**
 * HomeTab — 落地页(helixa 首页版式,清爽聚焦版)。
 * 结构:KPI 概览条 → 策略切换 → 状态条 → 资金曲线/持仓(60/40 hero)→ 市场速览
 * (FGI/regime/共识摘要)→ 事件流。重的每策略明细(统计/执行/信号/成交)默认折叠。
 * 全部真实数据,observe/paper,不接实盘拦截。
 */
'use client';

import { useState, type ReactNode } from 'react';
import { EmptyState, Skeleton, StaleBanner } from '../EmptyState';
import { EquityPanel } from '../charts';
import { helivexApi, portfolioApi, riskApi, ensembleApi, streamApi } from '@/lib/api-client';
import { useApi } from '@/lib/use-api';
import { SafeGateBadge } from '../SafeBadges';
import { EquityView, PositionsView, StatsView, ExecutionView, SignalsView, TradesView } from '../StrategyViews';
import type {
  StrategyState, PaperAccount, PortfolioSummary, RiskStatus, FgiResp,
  RegimeResp, ConsensusResp, TimelineResp, PortfolioEquity,
} from '@/types/api';

const STRAT_TAG: Record<string, string> = {
  spot_trend: '现货', scalp_5m: '日内', trend_dual: '趋势', vwap_mr_dual: '均值回归', ler_okx: 'LER',
  trend_follower_port: 'TF·移植', scalper_v2_port: 'Scalp2·移植', futures_signal_port: 'FS·移植',
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
      streamApi.fgi(), ensembleApi.regime(), ensembleApi.consensus(), portfolioApi.equity(),
    ]),
    [], 15000, 'home',
  );
  const tl = useApi<TimelineResp>(() => streamApi.timeline(20), [], 15000, 'home-tl');
  const [sel, setSel] = useState<string | null>(null);
  const [showDetail, setShowDetail] = useState(false);

  if (loading && !data) return <div className="hv-tab"><Skeleton /></div>;
  if (error && !data) return <div className="hv-tab"><EmptyState text="网关连接失败" sub={error} /></div>;
  const [strategies, account, summary, risk, fgi, regime, consensus, portfolioEq] =
    data as [StrategyState[], PaperAccount, PortfolioSummary, RiskStatus, FgiResp, RegimeResp, ConsensusResp, PortfolioEquity];
  const combinedPts = portfolioEq?.combined ?? [];

  // 默认选中首个有成交的策略——零成交策略的资金曲线/持仓/成交历史全是空态
  const id = sel
    ?? strategies.find(s => (s.n_fills ?? 0) > 0)?.strategy_id
    ?? strategies[0]?.strategy_id
    ?? null;
  const cur = strategies.find(s => s.strategy_id === id) ?? null;
  const tripped = risk.kill_switch.tripped;
  const positioned = !!cur && cur.position && cur.position !== '—' && cur.position !== 'flat';
  const consByInst: Record<string, ConsensusResp['consensus'][number]> = {};
  for (const c of consensus.consensus) consByInst[c.instrument] = c;

  const kpi = (label: string, value: ReactNode) => (
    <div className="hv-bar-item"><span className="hv-bar-label">{label}</span><span className="hv-bar-val">{value}</span></div>
  );

  return (
    <div className="hv-tab hv-home">
      {stale && <StaleBanner error={error!} />}

      {/* 概览 KPI 条 */}
      <div className="hv-bar">
        {kpi('净值 NAV', `$${risk.nav.toFixed(0)}`)}
        {kpi('今日盈亏', <span style={{ color: pnlColor(account.pnl_today_net) }}>${account.pnl_today_net.toFixed(2)}</span>)}
        {kpi('回撤', <span style={{ color: risk.drawdown_pct > risk.caps.max_drawdown_pct * 0.66 ? 'var(--destructive)' : undefined }}>{risk.drawdown_pct.toFixed(2)}%</span>)}
        {kpi('持仓', String(summary.total_positions))}
        {kpi('未实现', <span style={{ color: pnlColor(summary.total_unrealized_pnl) }}>${summary.total_unrealized_pnl.toFixed(2)}</span>)}
        {kpi('已实现', <span style={{ color: pnlColor(summary.total_realized_pnl) }}>${summary.total_realized_pnl.toFixed(2)}</span>)}
        <span className="hv-bar-note" style={{ color: tripped ? 'var(--destructive)' : 'var(--success,#3fb950)' }}>
          {tripped ? '🛑 已熔断' : '✅ 风控正常'}
        </span>
      </div>

      {/* 策略切换 */}
      <div className="hv-strat-tabs">
        {strategies.map(s => (
          <button key={s.strategy_id} className="hv-strat-tab"
            data-active={(id === s.strategy_id) ? 'true' : undefined}
            onClick={() => setSel(s.strategy_id)}>{tag(s.strategy_id)}</button>
        ))}
      </div>

      {/* 状态条 */}
      {cur && (
        <div className="hv-status">
          <span className="hv-status__dot" style={{ background: positioned ? 'var(--success,#3fb950)' : 'var(--muted-foreground)' }} />
          <span className="hv-status__name">{cur.name}</span>
          <SafeGateBadge verdict={cur.gate.verdict} dsr={cur.gate.dsr} pbo={cur.gate.pbo} />
          <span className="hv-status__meta">
            {positioned ? <>持仓 · <strong>{cur.position}</strong></> : '观望(无持仓)'}
          </span>
          <span className="hv-status__meta">今日信号 <strong>{cur.signals_today}</strong></span>
          <span className="hv-status__meta">regime <strong>{cur.regime}</strong></span>
        </div>
      )}

      {/* Hero:组合资金曲线(左,全策略合并 — 对齐 Hyperliquid 组合页"账户级优先")+ 持仓(右) */}
      <div className="hv-hero">
        <div className="hv-panel">
          {combinedPts.length < 2
            ? <EmptyState text="数据不足" sub="需 ≥2 个成交点" />
            : <EquityPanel pts={combinedPts} title="组合资金曲线 · 全策略合并" h={250} />}
        </div>
        {id && (
          <div className="hv-panel">
            <div className="hv-panel__head"><span className="hv-panel__title">持仓 — {cur?.name}</span></div>
            <PositionsView id={id} />
          </div>
        )}
      </div>

      {/* 市场速览:FGI + 每标的 regime/共识(摘要,详见 Ensemble) */}
      <div className="hv-panel">
        <div className="hv-panel__head">
          <span className="hv-panel__title">市场速览</span>
          <span className="hv-panel__link">引擎投票 / K线 / 决策轨迹详见 Ensemble</span>
        </div>
        <div className="hv-grid-3">
          {fgi.value != null && (
            <div className="hv-mini">
              <span className="hv-mini__k">Fear & Greed · 逆向</span>
              <span className="hv-mini__v" style={{ color: pnlColor(fgi.contrarian_bias) }}>
                {fgi.value.toFixed(0)} · {fgi.classification}
              </span>
            </div>
          )}
          {regime.regimes.map(r => {
            const c = consByInst[r.instrument];
            return (
              <div key={r.instrument} className="hv-mini">
                <span className="hv-mini__k">{sym(r.instrument)}</span>
                <span className="hv-mini__v" style={{ fontSize: 'var(--text-sm)' }}>
                  <span style={{ color: regimeColor(r.state) }}>{r.state}</span>
                  {c && <> · <span style={{ color: dirColor(c.final_direction) }}>{c.final_direction}</span>{c.should_execute ? ' ✅' : ''}</>}
                </span>
              </div>
            );
          })}
        </div>
      </div>

      {/* 事件流 */}
      <div className="hv-panel">
        <div className="hv-panel__head"><span className="hv-panel__title">事件流 · 成交 / 风控 / 共识</span></div>
        {(tl.data?.events ?? []).length === 0 ? <EmptyState text="暂无事件" /> : (
          <div className="hv-feed">
            {tl.data!.events.slice(0, 10).map((e, i) => (
              <div key={i} className="hv-feed__row">
                <span className="hv-feed__t">{new Date(e.ts).toLocaleTimeString()}</span>
                <span className="hv-feed__cat" style={{ color: e.category.startsWith('risk') ? 'var(--destructive)' : e.category === 'fill' ? 'var(--muted-foreground)' : 'oklch(0.70 0.12 240)' }}>{e.category}</span>
                <span className="hv-feed__label">{e.label}</span>
              </div>
            ))}
          </div>
        )}
      </div>

      {/* 策略明细(默认折叠,保持首页清爽) */}
      {id && (
        <>
          <button className="hv-collapse" onClick={() => setShowDetail(v => !v)} aria-expanded={showDetail}>
            {showDetail ? '▾ 收起策略明细' : '▸ 展开策略明细(资金曲线 / 统计 / 执行 / 信号 / 成交)'}
          </button>
          {showDetail && (
            <div className="hv-home__group">
              <div className="hv-flat-col"><div className="hv-section-title">资金曲线 — {cur?.name}</div><EquityView id={id} /></div>
              <div className="hv-grid-2">
                <div className="hv-flat-col"><div className="hv-section-title">统计</div><StatsView id={id} /></div>
                <div className="hv-flat-col"><div className="hv-section-title">执行质量(真实滑点/延迟)</div><ExecutionView id={id} /></div>
              </div>
              <div className="hv-grid-2">
                <div className="hv-flat-col"><div className="hv-section-title">信号历史</div><SignalsView id={id} /></div>
                <div className="hv-flat-col"><div className="hv-section-title">成交历史</div><TradesView id={id} /></div>
              </div>
            </div>
          )}
        </>
      )}
    </div>
  );
}
