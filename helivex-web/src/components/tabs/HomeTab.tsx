/**
 * HomeTab — 落地页(helixa 首页版式,清爽聚焦版)。
 * 结构:KPI 概览条 → 策略切换 → 状态条 → 资金曲线/持仓(60/40 hero)→ 市场速览
 * (FGI/regime/共识摘要)→ 事件流。重的每策略明细(统计/执行/信号/成交)默认折叠。
 * 全部真实数据,observe/paper,不接实盘拦截。
 */
'use client';

import { useState, type ReactNode } from 'react';
import { EmptyState, Skeleton, StaleBanner } from '../EmptyState';
import { EquityPanel, Underwater, DivergingBars } from '../charts';
import { helivexApi, portfolioApi, riskApi, ensembleApi, streamApi, detailApi } from '@/lib/api-client';
import { useApi } from '@/lib/use-api';
import { SafeGateBadge } from '../SafeBadges';
import { EquityView, PositionsView, StatsView, ExecutionView, SignalsView, TradesView } from '../StrategyViews';
import type {
  StrategyState, PaperAccount, PortfolioSummary, RiskStatus, FgiResp,
  RegimeResp, ConsensusResp, TimelineResp, PortfolioEquity,
  CorrelationMatrix, PortfolioAttributionResp, EquitySeriesPoint, StrategyEquity,
} from '@/types/api';

const shortStrat = (s: string) => s.replace(/_usdt_swap_okx$/, '').replace(/_/g, ' ');
const corrColor = (v: number) => {
  if (v >= 0.99) return 'var(--muted)';
  const abs = Math.abs(v);
  return abs < 0.3 ? 'color-mix(in oklch, var(--success, oklch(0.62 0.18 145)) 30%, transparent)'
    : abs < 0.6 ? 'color-mix(in oklch, oklch(0.70 0.15 80) 30%, transparent)'
    : 'color-mix(in oklch, var(--destructive) 30%, transparent)';
};

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

// hero 资金曲线选择:'组合'(全策略合并)或某个 strategy_id
const COMBINED = '__combined__';

// 组合视图:全策略合并曲线(数据随首页批量请求一并到手)
function CombinedEquity({ pts }: { pts: EquitySeriesPoint[] }) {
  if (pts.length < 2) return <EmptyState text="数据不足" sub="需 ≥2 个成交点" />;
  return (
    <>
      <EquityPanel pts={pts} title="组合资金曲线 · 全策略合并" h={210} />
      <Underwater pts={pts.map(p => p.drawdown ?? 0)} h={56} />
    </>
  );
}

// 单策略视图:自取该策略资金曲线(与折叠区 EquityView 同源 /strategies/{id}/equity)
function StrategyHeroEquity({ id, name }: { id: string; name?: string }) {
  const { data, loading, error } = useApi<StrategyEquity>(() => detailApi.equity(id), [id], 30000, `hero-eq:${id}`);
  if (loading && !data) return <Skeleton />;
  if (error && !data) return <EmptyState text="加载失败" sub={error} />;
  const pts = data?.points ?? [];
  if (pts.length < 2) return <EmptyState text="数据不足" sub="需 ≥2 个成交点" />;
  return (
    <>
      <EquityPanel pts={pts} title={`资金曲线 — ${name ?? id}`} h={210} />
      <Underwater pts={pts.map(p => p.drawdown ?? 0)} h={56} />
    </>
  );
}

export function HomeTab() {
  const { data, loading, error, stale } = useApi(
    () => Promise.all([
      helivexApi.strategies(), helivexApi.account(), portfolioApi.summary(), riskApi.status(),
      streamApi.fgi(), ensembleApi.regime(), ensembleApi.consensus(), portfolioApi.equity(),
      portfolioApi.correlation(),
    ]),
    [], 15000, 'home',
  );
  const tl = useApi<TimelineResp>(() => streamApi.timeline(20), [], 15000, 'home-tl');
  const attr = useApi<PortfolioAttributionResp>(() => portfolioApi.attribution(), [], 30000, 'home-attr');
  const [sel, setSel] = useState<string>(COMBINED);
  const [showDetail, setShowDetail] = useState(false);
  const [killConfirm, setKillConfirm] = useState(false);
  const [killed, setKilled] = useState(false);

  if (loading && !data) return <div className="hv-tab"><Skeleton /></div>;
  if (error && !data) return <div className="hv-tab"><EmptyState text="网关连接失败" sub={error} /></div>;
  const [strategies, account, summary, risk, fgi, regime, consensus, portfolioEq, corr] =
    data as [StrategyState[], PaperAccount, PortfolioSummary, RiskStatus, FgiResp, RegimeResp, ConsensusResp, PortfolioEquity, CorrelationMatrix];
  const combinedPts = portfolioEq?.combined ?? [];
  // 左上「净值」= 组合净值 = ∑各策略净值(组合曲线末值)。回退到 risk.nav(真实账户 NAV)。
  const navTotal = combinedPts.length ? combinedPts[combinedPts.length - 1].equity : risk.nav;
  const doKill = async () => {
    try { await portfolioApi.kill(); setKilled(true); } catch { /* surfaced below */ }
    setKillConfirm(false);
  };

  // 默认落在「组合」;选中某策略时 id 才是该策略,驱动状态条/持仓/明细
  const id = sel === COMBINED ? null : sel;
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
        {kpi('净值 NAV', `$${navTotal.toLocaleString('en-US', { maximumFractionDigits: 0 })}`)}
        {kpi('今日盈亏', <span style={{ color: pnlColor(account.pnl_today_net) }}>${account.pnl_today_net.toFixed(2)}</span>)}
        {kpi('回撤', <span style={{ color: risk.drawdown_pct > risk.caps.max_drawdown_pct * 0.66 ? 'var(--destructive)' : undefined }}>{risk.drawdown_pct.toFixed(2)}%</span>)}
        {kpi('持仓', String(summary.total_positions))}
        {kpi('未实现', <span style={{ color: pnlColor(summary.total_unrealized_pnl) }}>${summary.total_unrealized_pnl.toFixed(2)}</span>)}
        {kpi('已实现', <span style={{ color: pnlColor(summary.total_realized_pnl) }}>${summary.total_realized_pnl.toFixed(2)}</span>)}
        {kpi('可用资金', `$${summary.available?.toLocaleString() ?? '—'}`)}
        <span className="hv-bar-note" style={{ color: tripped ? 'var(--destructive)' : 'var(--success,#3fb950)' }}>
          {tripped ? '🛑 已熔断' : '✅ 风控正常'}
        </span>
      </div>

      {/* 策略切换(最前为「组合」= 全策略合并) */}
      <div className="hv-strat-tabs">
        <button className="hv-strat-tab"
          data-active={(sel === COMBINED) ? 'true' : undefined}
          onClick={() => setSel(COMBINED)}>组合</button>
        {strategies.map(s => (
          <button key={s.strategy_id} className="hv-strat-tab"
            data-active={(sel === s.strategy_id) ? 'true' : undefined}
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

      {/* Hero:资金曲线(左,可在「组合」与单策略间切换)+ 持仓(右,选中策略时) */}
      <div className="hv-hero" data-single={sel === COMBINED ? 'true' : undefined}>
        <div className="hv-panel">
          {sel === COMBINED
            ? <CombinedEquity pts={combinedPts} />
            : <StrategyHeroEquity id={sel} name={cur?.name} />}
        </div>
        {id && (
          <div className="hv-panel">
            <div className="hv-panel__head"><span className="hv-panel__title">持仓 — {cur?.name}</span></div>
            <PositionsView id={id} />
          </div>
        )}
      </div>

      {/* 组合分析:P&L 归因 + 策略相关性(原 Portfolio 页去重合并) */}
      <div className="hv-grid-2">
        <div className="hv-panel">
          <div className="hv-panel__head">
            <span className="hv-panel__title">P&L 归因(按策略)</span>
            <span className="hv-panel__link">合计已实现 {attr.data ? `$${attr.data.total_realized.toFixed(2)}` : '—'}</span>
          </div>
          {(attr.data?.by_strategy ?? []).length === 0 ? <EmptyState text="暂无归因数据" sub="需已平仓的回合" /> : (
            <>
              <DivergingBars items={attr.data!.by_strategy.map(s => ({ label: shortStrat(s.strategy_id), value: s.realized_pnl, ok: s.realized_pnl >= 0 }))} unit="$" />
              <table className="hv-table" aria-label="P&L 归因">
                <thead><tr><th>策略</th><th>已实现</th><th>成交数</th><th>胜率</th><th>最佳/最差</th></tr></thead>
                <tbody>
                  {attr.data!.by_strategy.map(s => (
                    <tr key={s.strategy_id}>
                      <td>{shortStrat(s.strategy_id)}</td>
                      <td style={{ color: s.realized_pnl >= 0 ? 'var(--success,#3fb950)' : 'var(--destructive)' }}>${s.realized_pnl.toFixed(2)}</td>
                      <td>{s.n_trades}</td>
                      <td>{s.win_rate != null ? (s.win_rate * 100).toFixed(0) + '%' : '—'}</td>
                      <td className="hv-num">{s.best != null ? `+${s.best}` : '—'} / {s.worst != null ? s.worst : '—'}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
              <div className="hv-honest-note">⚠️ Paper 短期 P&L ≠ 策略有效。归因按<strong>策略</strong>(引擎级归因要等 enforce 后引擎驱动执行才成立)。</div>
            </>
          )}
        </div>
        <div className="hv-panel">
          <div className="hv-panel__head">
            <span className="hv-panel__title">策略相关性</span>
            <span className="hv-panel__link">低相关 = 分散好</span>
          </div>
          {!corr?.matrix?.length ? <EmptyState text="暂无相关性数据" /> : (
            <div className="hv-corr-matrix">
              <table className="hv-table" aria-label="策略相关性矩阵">
                <thead><tr><th></th>{corr.strategies.map(s => <th key={s} className="hv-num">{shortStrat(s)}</th>)}</tr></thead>
                <tbody>
                  {corr.matrix.map((row, i) => (
                    <tr key={i}>
                      <td>{corr.strategies[i] ? shortStrat(corr.strategies[i]) : i}</td>
                      {row.map((v, j) => <td key={j} className="hv-num" style={{ background: corrColor(v), textAlign: 'center' }}>{v.toFixed(2)}</td>)}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
          <div className="hv-honest-note">低相关性利于组合分散。边界策略组合可能整体过 gate(R4)。</div>
        </div>
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

      {/* 风险控制(原 Portfolio 页) */}
      <div className="hv-section-title">风险控制</div>
      {killed ? (
        <div className="hv-honest-note">已发送停止指令。</div>
      ) : !killConfirm ? (
        <button className="hv-kill-btn" onClick={() => setKillConfirm(true)}>⏹ 一键停所有策略</button>
      ) : (
        <div className="hv-kill-confirm">
          <span>确定停止所有策略?这会平掉所有 paper 持仓。</span>
          <div className="hv-kill-actions">
            <button className="hv-kill-cancel" onClick={() => setKillConfirm(false)}>取消</button>
            <button className="hv-kill-confirm-btn" onClick={doKill}>确认停止</button>
          </div>
        </div>
      )}
    </div>
  );
}
