/**
 * OverviewTab — 系统总览(真实数据,无 mock)
 * 真实账户 + 真实策略状态(gate verdict 真实)+ 真实审计链 health
 */
'use client';

import { SafeGateBadge, SafeRegimeBadge } from '../SafeBadges';
import { EmptyState, Skeleton } from '../EmptyState';
import { helivexApi } from '@/lib/api-client';
import { useApi } from '@/lib/use-api';
import type { StrategyState, PaperAccount } from '@/types/api';

interface ChainHealthReal { ok: boolean; n_total: number; n_gold: number; n_valid: number; }

const MODE_COLOR: Record<string, string> = {
  shadow: 'var(--muted-foreground)', paper: 'oklch(0.62 0.16 200)', live: 'oklch(0.60 0.22 25)',
};
const sign = (v: number) => (v > 0 ? 'var(--success,#3fb950)' : v < 0 ? 'var(--destructive)' : 'var(--muted-foreground)');
const fmtUsd = (v: number) => (v >= 0 ? '+' : '−') + '$' + Math.abs(v).toLocaleString(undefined, { maximumFractionDigits: 2 });

export function OverviewTab() {
  const { data, loading, error } = useApi(
    () => Promise.all([helivexApi.strategies(), helivexApi.account(), helivexApi.chainHealth() as unknown as Promise<ChainHealthReal>]),
    [], 15000,
  );
  if (loading) return <div className="hv-tab"><Skeleton /></div>;
  if (error) return <div className="hv-tab"><EmptyState text="网关连接失败" sub={error} /></div>;
  const [strategies, account, chain] = data as [StrategyState[], PaperAccount, ChainHealthReal];

  return (
    <div className="hv-tab">
      {/* chain health banner — 真实 */}
      <div className="hv-chain-banner" data-intact={chain?.ok ? 'true' : undefined}>
        <span className="hv-chain-icon">{chain?.ok ? '✓' : '✕'}</span>
        <div className="hv-chain-text">
          <strong>审计链{chain?.ok ? '完整' : '断裂'}</strong>
          <span>GOLD 签名 {chain?.n_gold ?? 0}/{chain?.n_total ?? 0} · 验签通过 {chain?.n_valid ?? 0}</span>
        </div>
      </div>

      <div className="hv-section-title">策略状态</div>
      {(strategies ?? []).length === 0 ? <EmptyState text="暂无策略" /> : (
        <div className="hv-grid-3">
          {strategies.map(s => (
            <div key={s.strategy_id} className="hv-strat-card">
              <div className="hv-strat-card__head">
                <span className="hv-strat-name">{s.name}</span>
                <span className="hv-mode-badge" style={{ color: MODE_COLOR[s.mode] }}>{s.mode}</span>
              </div>
              <div className="hv-strat-card__row">
                <SafeRegimeBadge regime={s.regime} />
                <SafeGateBadge verdict={s.gate?.verdict} dsr={s.gate?.dsr} pbo={s.gate?.pbo} compact />
              </div>
              <div className="hv-strat-card__meta">
                <span>持仓: {s.position}</span>
                <span>今日信号: {s.signals_today}</span>
              </div>
            </div>
          ))}
        </div>
      )}

      <div className="hv-section-title">Paper 账户</div>
      <div className="hv-grid-3">
        <div className="hv-metric-card">
          <span className="hv-metric-label">余额</span>
          <span className="hv-metric-val">${account?.balance?.toLocaleString(undefined, { maximumFractionDigits: 2 }) ?? '—'}</span>
        </div>
        <div className="hv-metric-card">
          <span className="hv-metric-label">今日 P&L (gross)</span>
          <span className="hv-metric-val" style={{ color: sign(account?.pnl_today_gross ?? 0) }}>{account ? fmtUsd(account.pnl_today_gross) : '—'}</span>
        </div>
        <div className="hv-metric-card">
          <span className="hv-metric-label">今日 P&L (net)</span>
          <span className="hv-metric-val" style={{ color: sign(account?.pnl_today_net ?? 0) }}>{account ? fmtUsd(account.pnl_today_net) : '—'}</span>
          <span className="hv-metric-note">net &lt; gross(手续费/滑点)</span>
        </div>
      </div>

      <div className="hv-honest-note">
        ⚠️ Paper 短期 P&L ≠ 策略有效(含运气成分),需配合执行真实度 + 足够长样本判断。当前 gate 全 FAIL/NO-GO。
      </div>
    </div>
  );
}
