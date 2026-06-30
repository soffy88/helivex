/**
 * RiskTab — R14 组合风控层可视化(kill-switch / 回撤 vs 上限 / 日亏 vs 限额 / 风控事件)
 * MicrostructureTab — R16 L2 盘口微观结构(spread / imbalance / microprice,实时)
 * 全部真实数据;录制器无数据走诚实空状态。
 */
'use client';

import { useState } from 'react';
import { EmptyState, Skeleton, StaleBanner } from '../EmptyState';
import { riskApi, microApi } from '@/lib/api-client';
import { useApi } from '@/lib/use-api';
import { Sparkline } from '../charts';
import type { RiskStatus, RiskEvent, MicroLatest, MicroSeriesPoint } from '@/types/api';

const usd = (v: number) => (v >= 0 ? '+' : '−') + '$' + Math.abs(v).toLocaleString(undefined, { maximumFractionDigits: 2 });
const sevColor = (s: string) => s === 'critical' ? 'var(--destructive)' : s === 'high' ? 'oklch(0.70 0.15 80)' : 'var(--muted-foreground)';

/** utilization bar: value vs cap, fills + turns red as it approaches the cap */
function UtilBar({ value, cap, invert }: { value: number; cap: number; invert?: boolean }) {
  const pct = Math.max(0, Math.min(100, (Math.abs(value) / cap) * 100));
  const color = pct >= 90 ? 'var(--destructive)' : pct >= 60 ? 'oklch(0.70 0.15 80)' : 'var(--success, oklch(0.62 0.18 145))';
  return (
    <div className="hv-util">
      <div className="hv-util-fill" style={{ width: `${pct}%`, background: invert ? color : color }} />
      <span className="hv-util-label">{pct.toFixed(0)}%</span>
    </div>
  );
}

/** order-book imbalance bar, centered at 0, value in [-1, 1] (green=bid, red=ask) */
function ImbalanceBar({ v }: { v: number }) {
  const half = Math.min(50, Math.abs(v) * 50);
  const pos = v >= 0;
  return (
    <div className="hv-imb">
      <div className="hv-imb-center" />
      <div className="hv-imb-fill" style={{
        left: pos ? '50%' : `${50 - half}%`, width: `${half}%`,
        background: pos ? 'var(--success, oklch(0.62 0.18 145))' : 'var(--destructive)',
      }} />
    </div>
  );
}

// ── Risk Tab ──────────────────────────────────────────────────────────────────

export function RiskTab() {
  const { data, loading, error, stale } = useApi(
    () => Promise.all([riskApi.status(), riskApi.events()]),
    [], 2000, 'risk',
  );
  const [confirm, setConfirm] = useState(false);
  const [busy, setBusy] = useState(false);

  if (loading && !data) return <div className="hv-tab"><Skeleton /></div>;
  if (error && !data) return <div className="hv-tab"><EmptyState text="网关连接失败" sub={error} /></div>;
  const [st, events] = data as [RiskStatus, RiskEvent[]];
  const ks = st.kill_switch;

  const doTrip = async () => { setBusy(true); try { await riskApi.kill('manual trip via dashboard'); } finally { setBusy(false); setConfirm(false); } };
  const doReset = async () => { setBusy(true); try { await riskApi.reset(); } finally { setBusy(false); } };

  return (
    <div className="hv-tab">
      {stale && <StaleBanner error={error!} />}

      {/* kill-switch status banner */}
      <div className="hv-ks-banner" data-tripped={ks.tripped ? 'true' : undefined}>
        <span className="hv-ks-icon">{ks.tripped ? '⏹' : '●'}</span>
        <div className="hv-ks-text">
          <strong>{ks.tripped ? 'Kill-switch 已触发 — 拒绝新开仓' : 'Kill-switch 正常 — 允许开仓'}</strong>
          {ks.tripped && <span>{ks.reason}</span>}
        </div>
        {ks.tripped
          ? <button className="hv-btn-recover" disabled={busy} onClick={doReset}>复位</button>
          : !confirm
            ? <button className="hv-btn-soft" disabled={busy} onClick={() => setConfirm(true)}>软停(停新开仓)</button>
            : (<span className="hv-kill-actions">
                <button className="hv-kill-cancel" onClick={() => setConfirm(false)}>取消</button>
                <button className="hv-kill-confirm-btn" disabled={busy} onClick={doTrip}>确认软停</button>
              </span>)}
      </div>

      {/* NAV + breakers */}
      <div className="hv-section-title">组合 NAV 与熔断</div>
      <div className="hv-grid-4">
        <div className="hv-metric-card"><span className="hv-metric-label">NAV(base + 已实现)</span><span className="hv-metric-val">${st.nav.toLocaleString()}</span></div>
        <div className="hv-metric-card"><span className="hv-metric-label">峰值</span><span className="hv-metric-val">${st.peak.toLocaleString()}</span></div>
        <div className="hv-metric-card"><span className="hv-metric-label">今日已实现</span><span className="hv-metric-val" style={{ color: st.realized_today >= 0 ? 'var(--success,#3fb950)' : 'var(--destructive)' }}>{usd(st.realized_today)}</span></div>
        <div className="hv-metric-card"><span className="hv-metric-label">累计已实现</span><span className="hv-metric-val" style={{ color: st.realized_all >= 0 ? 'var(--success,#3fb950)' : 'var(--destructive)' }}>{usd(st.realized_all)}</span></div>
      </div>

      <div className="hv-grid-2">
        <div className="hv-flat-col">
          <div className="hv-metric-label">回撤 vs 熔断线 ({st.drawdown_pct.toFixed(2)}% / {st.caps.max_drawdown_pct}%)</div>
          <UtilBar value={st.drawdown_pct} cap={st.caps.max_drawdown_pct} />
        </div>
        <div className="hv-flat-col">
          <div className="hv-metric-label">今日亏损 vs 限额 ({Math.abs(Math.min(0, st.realized_today)).toFixed(0)} / {st.caps.daily_loss_limit_usd})</div>
          <UtilBar value={Math.min(0, st.realized_today)} cap={st.caps.daily_loss_limit_usd} />
        </div>
      </div>

      {/* caps */}
      <div className="hv-section-title">预交易硬上限(R14)</div>
      <div className="hv-grid-4">
        <div className="hv-metric-card"><span className="hv-metric-label">组合 gross</span><span className="hv-metric-val">${st.caps.portfolio_gross_usd.toLocaleString()}</span></div>
        <div className="hv-metric-card"><span className="hv-metric-label">单策略</span><span className="hv-metric-val">${st.caps.per_strategy_usd.toLocaleString()}</span></div>
        <div className="hv-metric-card"><span className="hv-metric-label">单标的</span><span className="hv-metric-val">${st.caps.per_instrument_usd.toLocaleString()}</span></div>
        <div className="hv-metric-card"><span className="hv-metric-label">最大持仓数</span><span className="hv-metric-val">{st.caps.max_positions}</span></div>
      </div>
      <div className="hv-honest-note">回撤基于已实现 P&L(未标记未实现持仓);kill-switch 跨进程文件标志,monitor 每 120s 检查。软停只拦新开仓,平仓永远放行。</div>

      {/* events */}
      <div className="hv-section-title">风控事件(paper.risk_events)</div>
      {events.length === 0 ? <EmptyState text="暂无风控事件" sub="越界 / 触发 / 复位 都会记录于此" /> : (
        <table className="hv-table" aria-label="风控事件">
          <thead><tr><th>时间</th><th>类型</th><th>实体</th><th>级别</th><th>消息</th></tr></thead>
          <tbody>
            {events.map((e, i) => (
              <tr key={i}>
                <td>{new Date(e.ts).toLocaleString()}</td>
                <td>{e.kind}</td>
                <td>{e.entity_id}</td>
                <td style={{ color: sevColor(e.severity) }}>{e.severity}</td>
                <td style={{ fontSize: 'var(--text-xs)' }}>{e.message}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}

// ── Microstructure Tab ──────────────────────────────────────────────────────────

export function MicrostructureTab() {
  const { data, loading, error, stale } = useApi<MicroLatest>(() => microApi.latest(), [], 2000, 'micro');
  if (loading && !data) return <div className="hv-tab"><Skeleton /></div>;
  if (error && !data) return <div className="hv-tab"><EmptyState text="网关连接失败" sub={error} /></div>;
  const latest = data?.latest ?? [];
  const series = data?.series ?? {};

  if (latest.length === 0) {
    return <div className="hv-tab"><EmptyState text="L2 录制器暂无数据" sub="helivex-l2recorder 启动后即开始写入 market_data.orderbook_features" /></div>;
  }

  return (
    <div className="hv-tab">
      {stale && <StaleBanner error={error!} />}
      <div className="hv-section-title">L2 盘口微观结构(R16,实时 · OKX books via 代理)</div>
      <div className="hv-grid-3">
        {latest.map(m => {
          const s = series[m.instrument] ?? [];
          const imbSeries = s.map((p: MicroSeriesPoint) => p.imbalance1);
          const midSeries = s.map((p: MicroSeriesPoint) => p.mid);
          const sprSeries = s.map((p: MicroSeriesPoint) => p.spread_bps);
          return (
            <div key={m.instrument} className="hv-micro-card">
              <div className="hv-micro-head">
                <span className="hv-strat-name">{m.instrument.split('-')[0]}</span>
                <span className="hv-micro-time">{new Date(m.ts).toLocaleTimeString()}</span>
              </div>
              <div className="hv-micro-mid">{m.mid.toLocaleString(undefined, { maximumFractionDigits: 2 })}</div>
              <div className="hv-micro-row"><span>spread</span><span className="hv-num">{m.spread_bps.toFixed(3)} bps</span></div>
              <div className="hv-micro-row"><span>microprice</span><span className="hv-num">{m.microprice.toLocaleString(undefined, { maximumFractionDigits: 2 })}</span></div>
              <div className="hv-micro-row"><span>L1 量 (bid/ask)</span><span className="hv-num">{m.bid_sz1.toFixed(2)} / {m.ask_sz1.toFixed(2)}</span></div>
              <div className="hv-micro-row"><span>5 档深度 (bid/ask)</span><span className="hv-num">{m.bid_depth5.toFixed(1)} / {m.ask_depth5.toFixed(1)}</span></div>
              <div className="hv-micro-imb-label">L1 盘口不平衡 <strong style={{ color: m.imbalance1 >= 0 ? 'var(--success,#3fb950)' : 'var(--destructive)' }}>{m.imbalance1.toFixed(3)}</strong></div>
              <ImbalanceBar v={m.imbalance1} />
              <div className="hv-micro-imb-label">L5 盘口不平衡 <strong style={{ color: m.imbalance5 >= 0 ? 'var(--success,#3fb950)' : 'var(--destructive)' }}>{m.imbalance5.toFixed(3)}</strong></div>
              <ImbalanceBar v={m.imbalance5} />
              <div className="hv-micro-spark"><span>mid 走势</span><Sparkline pts={midSeries} color="var(--foreground)" /></div>
              <div className="hv-micro-spark"><span>spread bps</span><Sparkline pts={sprSeries} color="oklch(0.70 0.15 80)" /></div>
              <div className="hv-micro-spark"><span>L1 imbalance</span><Sparkline pts={imbSeries} color="var(--primary)" /></div>
            </div>
          );
        })}
      </div>
      <div className="hv-honest-note">⚠️ 前向投资(R16):L2 微观结构刚开始积累,暂无回测。攒够数周后将探针盘口不平衡 / microprice 作短周期预测因子,再决定是否上 gate。</div>
    </div>
  );
}
