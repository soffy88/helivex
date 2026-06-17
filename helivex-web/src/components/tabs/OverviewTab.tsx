/**
 * OverviewTab — 系统总览(P1)
 * 三策略状态卡 + chain health + paper account + 系统健康
 */
'use client';

import { SafeGateBadge, SafeRegimeBadge } from '../SafeBadges';
import { MOCK_STRATEGIES, MOCK_CHAIN, MOCK_ACCOUNT } from '@/lib/mock-data';

const MODE_COLOR: Record<string, string> = {
  shadow: 'var(--muted-foreground)', paper: 'oklch(0.62 0.16 200)', live: 'oklch(0.60 0.22 25)',
};

export function OverviewTab() {
  return (
    <div className="hv-tab">
      {/* chain health banner */}
      <div className="hv-chain-banner" data-intact={MOCK_CHAIN.intact ? 'true' : undefined}>
        <span className="hv-chain-icon">{MOCK_CHAIN.intact ? '✓' : '✕'}</span>
        <div className="hv-chain-text">
          <strong>审计链{MOCK_CHAIN.intact ? '完整' : '断裂'}</strong>
          <span>GOLD 签名 {MOCK_CHAIN.gold_signed ? '正常' : '异常'} · 最新 anchor {MOCK_CHAIN.latest_anchor}</span>
        </div>
      </div>

      {/* 三策略状态卡 */}
      <div className="hv-section-title">策略状态</div>
      <div className="hv-grid-3">
        {(MOCK_STRATEGIES ?? []).map(s => (
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

      {/* paper account */}
      <div className="hv-section-title">Paper 账户</div>
      <div className="hv-grid-3">
        <div className="hv-metric-card">
          <span className="hv-metric-label">余额</span>
          <span className="hv-metric-val">${MOCK_ACCOUNT.balance.toLocaleString()}</span>
        </div>
        <div className="hv-metric-card">
          <span className="hv-metric-label">今日 P&L (gross)</span>
          <span className="hv-metric-val hv-pos">+${MOCK_ACCOUNT.pnl_today_gross}</span>
        </div>
        <div className="hv-metric-card">
          <span className="hv-metric-label">今日 P&L (net)</span>
          <span className="hv-metric-val hv-pos">+${MOCK_ACCOUNT.pnl_today_net}</span>
          <span className="hv-metric-note">net &lt; gross(手续费/滑点)</span>
        </div>
      </div>

      {/* 诚实提示 */}
      <div className="hv-honest-note">
        ⚠️ Paper 短期 P&L ≠ 策略有效(含运气成分),需配合执行真实度 + 足够长样本判断。
      </div>
    </div>
  );
}
