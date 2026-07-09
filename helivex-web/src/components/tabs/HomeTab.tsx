/**
 * HomeTab — helixa 首页版式的完整复刻,接 helivex 网关(3O 替代,活数据)。
 * helixa 首页(quant-stack Dashboard)的面板:总览 KPI 条 · 策略切换 · 状态卡 ·
 * 资金曲线+持仓 · FGI/Regime/共识/引擎投票/阈值 · 事件流 · K线 · 决策轨迹 · 信号+成交流。
 * 显示 helivex 的 5 个替代策略(spot_trend/scalp_5m/trend_dual/vwap_mr_dual/ler_okx)——
 * 它们就是 helixa SPOT/SCALPER/TREND 等策略在 helivex 里的重实现;helixa 自身引擎已停用。
 * 全部真实数据,observe-only。
 */
'use client';

import { useState } from 'react';
import { EmptyState, Skeleton, StaleBanner } from '../EmptyState';
import { helivexApi, portfolioApi, riskApi, ensembleApi, streamApi, chartApi } from '@/lib/api-client';
import { useApi } from '@/lib/use-api';
import { Candlestick } from '../charts';
import { SafeGateBadge } from '../SafeBadges';
import { EquityView, PositionsView, StatsView, SignalsView, TradesView } from '../StrategyViews';
import type {
  StrategyState, PaperAccount, PortfolioSummary, RiskStatus, FgiResp,
  RegimeResp, EnginesResp, ConsensusResp, DecisionTrailResp, TimelineResp, OhlcvResp,
} from '@/types/api';

// helivex 策略 → helixa 风格短标签(现货/日内/趋势/均值回归/研究)
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
      streamApi.fgi(), ensembleApi.regime(), ensembleApi.engines(), ensembleApi.consensus(),
    ]),
    [], 15000, 'home',
  );
  const trail = useApi<DecisionTrailResp>(() => chartApi.decisionTrail(8), [], 20000, 'home-trail');
  const tl = useApi<TimelineResp>(() => streamApi.timeline(20), [], 15000, 'home-tl');
  const ohlcv = useApi<OhlcvResp>(() => chartApi.ohlcv('BTC-USDT', 120), [], 30000, 'home-ohlcv');
  const [sel, setSel] = useState<string | null>(null);

  if (loading && !data) return <div className="hv-tab"><Skeleton /></div>;
  if (error && !data) return <div className="hv-tab"><EmptyState text="网关连接失败" sub={error} /></div>;
  const [strategies, account, summary, risk, fgi, regime, engines, consensus] =
    data as [StrategyState[], PaperAccount, PortfolioSummary, RiskStatus, FgiResp, RegimeResp, EnginesResp, ConsensusResp];

  const id = sel ?? strategies[0]?.strategy_id ?? null;
  const cur = strategies.find(s => s.strategy_id === id) ?? null;
  const tripped = risk.kill_switch.tripped;

  return (
    <div className="hv-tab">
      {stale && <StaleBanner error={error!} />}

      {/* ── TotalKpiBar:跨策略总览 ── */}
      <div className="hv-grid-3">
        <div className="hv-metric-card"><span className="hv-metric-label">总净值 (NAV)</span>
          <span className="hv-metric-value">${risk.nav.toFixed(2)}</span></div>
        <div className="hv-metric-card"><span className="hv-metric-label">今日净盈亏</span>
          <span className="hv-metric-value" style={{ color: pnlColor(account.pnl_today_net) }}>${account.pnl_today_net.toFixed(2)}</span></div>
        <div className="hv-metric-card"><span className="hv-metric-label">回撤 (vs 峰值 ${risk.peak.toFixed(0)})</span>
          <span className="hv-metric-value" style={{ color: risk.drawdown_pct > risk.caps.max_drawdown_pct * 0.66 ? 'var(--destructive)' : 'var(--muted-foreground)' }}>{risk.drawdown_pct.toFixed(2)}%</span></div>
        <div className="hv-metric-card"><span className="hv-metric-label">总持仓</span>
          <span className="hv-metric-value">{summary.total_positions}</span></div>
        <div className="hv-metric-card"><span className="hv-metric-label">未实现 / 已实现</span>
          <span className="hv-metric-value" style={{ fontSize: 'var(--text-sm)' }}>
            <span style={{ color: pnlColor(summary.total_unrealized_pnl) }}>${summary.total_unrealized_pnl.toFixed(2)}</span>
            {' / '}
            <span style={{ color: pnlColor(summary.total_realized_pnl) }}>${summary.total_realized_pnl.toFixed(2)}</span>
          </span></div>
        <div className="hv-metric-card"><span className="hv-metric-label">熔断</span>
          <span className="hv-metric-value" style={{ color: tripped ? 'var(--destructive)' : 'var(--success,#3fb950)' }}>{tripped ? '🛑 已熔断' : '✅ 正常'}</span></div>
      </div>

      {/* ── StrategyTabs:策略切换(helixa 风格)── */}
      <div className="hv-strat-tabs" style={{ marginTop: 16 }}>
        {strategies.map(s => (
          <button key={s.strategy_id} className="hv-strat-tab"
            data-active={(id === s.strategy_id) ? 'true' : undefined}
            onClick={() => setSel(s.strategy_id)}>
            {tag(s.strategy_id)}
          </button>
        ))}
      </div>

      {/* ── StatusCard:选中策略一行状态 ── */}
      {cur && (
        <div className="hv-honest-note" style={{ display: 'flex', gap: 12, alignItems: 'center', flexWrap: 'wrap' }}>
          <strong>{cur.name}</strong>
          <SafeGateBadge verdict={cur.gate.verdict} dsr={cur.gate.dsr} pbo={cur.gate.pbo} />
          <span>状态:{cur.position && cur.position !== '—' && cur.position !== 'flat'
            ? <span style={{ color: 'var(--success,#3fb950)' }}>持仓中 · {cur.position}</span>
            : <span style={{ color: 'var(--muted-foreground)' }}>观望(无持仓)</span>}</span>
          <span>今日信号 {cur.signals_today}</span>
          <span style={{ color: 'var(--muted-foreground)' }}>regime {cur.regime}</span>
        </div>
      )}

      {/* ── 资金曲线 + 持仓(两栏)── */}
      {id && (
        <div className="hv-grid-2" style={{ marginTop: 8 }}>
          <div className="hv-flat-col"><div className="hv-section-title">资金曲线 — {cur?.name}</div><EquityView id={id} /></div>
          <div className="hv-flat-col"><div className="hv-section-title">持仓</div><PositionsView id={id} /></div>
        </div>
      )}

      {/* ── FGI + Regime(情绪 / 市场状态)── */}
      <div className="hv-section-title">市场情绪 & Regime</div>
      <div className="hv-grid-3">
        {fgi.value != null && (
          <div className="hv-metric-card">
            <span className="hv-metric-label">Fear & Greed · 逆向偏置</span>
            <span className="hv-metric-value" style={{ color: pnlColor(fgi.contrarian_bias) }}>
              {fgi.value.toFixed(0)} {fgi.classification} · {fgi.contrarian_bias > 0 ? '+' : ''}{fgi.contrarian_bias.toFixed(2)}
            </span>
          </div>
        )}
        {regime.regimes.map(r => (
          <div key={r.instrument} className="hv-metric-card">
            <span className="hv-metric-label">{sym(r.instrument)} regime</span>
            <span className="hv-metric-value" style={{ color: regimeColor(r.state) }}>
              {r.state}{r.confidence != null ? ` · ${(r.confidence * 100).toFixed(0)}%` : ''}
            </span>
          </div>
        ))}
      </div>

      {/* ── 共识(Consensus + Kelly + 阈值)── */}
      <div className="hv-section-title">共识大脑(只 promoted 引擎驱动)· enffce=observe</div>
      {consensus.consensus.length === 0 ? <EmptyState text="暂无共识" /> : (
        <table className="hv-table" aria-label="共识">
          <thead><tr><th>标的</th><th>方向</th><th>共识分</th><th>Kelly</th><th>一致度</th><th>#promoted</th><th>可执行</th></tr></thead>
          <tbody>
            {consensus.consensus.map(c => (
              <tr key={c.instrument}>
                <td>{sym(c.instrument)}</td>
                <td style={{ color: dirColor(c.final_direction) }}>{c.final_direction}</td>
                <td className="hv-num">{c.consensus_score != null ? c.consensus_score.toFixed(3) : '—'}</td>
                <td>{c.kelly_position != null ? (c.kelly_position * 100).toFixed(1) + '%' : '—'}</td>
                <td>{c.agreement_ratio != null ? (c.agreement_ratio * 100).toFixed(0) + '%' : '—'}</td>
                <td>{c.n_promoted}</td>
                <td style={{ color: c.should_execute ? 'var(--success,#3fb950)' : 'var(--muted-foreground)' }}>{c.should_execute ? '✅' : '观察'}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      {/* ── 引擎投票(EngineVotes)── */}
      <div className="hv-section-title">引擎投票(promoted=过自身门)</div>
      {engines.engines.length === 0 ? <EmptyState text="暂无引擎信号" /> : (
        <table className="hv-table" aria-label="引擎投票">
          <thead><tr><th>引擎</th><th>标的</th><th>方向</th><th>score</th><th>置信</th><th>门</th></tr></thead>
          <tbody>
            {engines.engines.map((e, i) => (
              <tr key={i}>
                <td>{e.engine}</td>
                <td>{sym(e.instrument)}</td>
                <td style={{ color: dirColor(e.direction) }}>{e.direction}</td>
                <td className="hv-num">{e.score != null ? e.score.toFixed(3) : '—'}</td>
                <td>{e.confidence != null ? (e.confidence * 100).toFixed(0) + '%' : '—'}</td>
                <td style={{ color: e.promoted ? 'var(--success,#3fb950)' : 'var(--muted-foreground)' }}>{e.promoted ? '✓ promoted' : '· observe'}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      {/* ── PriceChart:K线 + 成交标记 ── */}
      <div className="hv-section-title">K 线 · {ohlcv.data ? sym(ohlcv.data.instrument) : 'BTC'} 5m</div>
      {ohlcv.data && ohlcv.data.candles.length >= 2 ? (
        <Candlestick candles={ohlcv.data.candles} markers={ohlcv.data.markers} />
      ) : <EmptyState text="K 线数据不足" />}

      {/* ── StrategyStream:事件流 ── */}
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

      {/* ── DecisionTrail:决策轨迹 ── */}
      <div className="hv-section-title">决策轨迹(3O 指纹,可复现)</div>
      {(trail.data?.decision_trail ?? []).length === 0 ? <EmptyState text="暂无决策轨迹" /> : (
        <table className="hv-table" aria-label="决策轨迹">
          <thead><tr><th>时间</th><th>类型</th><th>标的</th><th>指纹</th></tr></thead>
          <tbody>
            {trail.data!.decision_trail.map((t, i) => (
              <tr key={i}>
                <td className="hv-num">{new Date(t.ts).toLocaleTimeString()}</td>
                <td>{t.kind}</td>
                <td>{t.instrument ? sym(t.instrument) : '—'}</td>
                <td style={{ fontFamily: 'monospace', fontSize: 'var(--text-xs)' }}>{(t.fingerprint || '—').slice(0, 12)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      {/* ── DataFeed:信号 + 成交历史 ── */}
      {id && (
        <div className="hv-grid-2" style={{ marginTop: 8 }}>
          <div className="hv-flat-col"><div className="hv-section-title">信号历史</div><SignalsView id={id} /></div>
          <div className="hv-flat-col"><div className="hv-section-title">成交历史</div><TradesView id={id} /></div>
        </div>
      )}
      {id && <><div className="hv-section-title">统计</div><StatsView id={id} /></>}

      <div className="hv-honest-note">
        helixa 首页版式复刻,接 helivex 网关活数据。显示 helivex 的 5 个替代策略(即 helixa
        SPOT/SCALPER/TREND 等在 helivex 的 3O 重实现);helixa 自身决策层已停用,不再产数。
        全部 <strong>observe-only</strong>,不接实盘。
      </div>
    </div>
  );
}
