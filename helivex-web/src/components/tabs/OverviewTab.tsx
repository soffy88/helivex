/**
 * OverviewTab — 系统总览(P1)
 * 三策略状态卡 + chain health + paper account + 系统健康
 * USE_MOCK=false 时从 api-gateway(:8765) 拉真实数据。
 */
'use client';

import { useEffect, useState } from 'react';
import { OGateBadge, ORegimeBadge } from '@helios/blocks';
import { MOCK_STRATEGIES, MOCK_CHAIN, MOCK_ACCOUNT } from '@/lib/mock-data';
import { helivexApi, USE_MOCK, mergeGatewayStrategy } from '@/lib/api-client';
import type { StrategyState, ChainHealth, PaperAccount } from '@/types/api';

const MODE_COLOR: Record<string, string> = {
  shadow: 'var(--muted-foreground)', paper: 'oklch(0.62 0.16 200)', live: 'oklch(0.60 0.22 25)',
};

const MOCK_BY_GW_ID: Record<string, StrategyState> = {
  trend_dual:   MOCK_STRATEGIES[0]!,
  vwap_mr_dual: MOCK_STRATEGIES[1]!,
  spot_trend:   MOCK_STRATEGIES[2]!,
};

export function OverviewTab() {
  const [strategies, setStrategies] = useState<StrategyState[]>(MOCK_STRATEGIES);
  const [chain, setChain] = useState<ChainHealth>(MOCK_CHAIN);
  const [account, setAccount] = useState<PaperAccount>(MOCK_ACCOUNT);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    if (USE_MOCK) return;
    setErr(null);
    Promise.all([
      helivexApi.strategies().then(rows =>
        setStrategies(rows.map(gw => mergeGatewayStrategy(MOCK_BY_GW_ID[gw.id] ?? MOCK_STRATEGIES[0]!, gw)))
      ),
      helivexApi.chainHealth().then(setChain),
      helivexApi.account().then(setAccount),
    ]).catch(e => setErr(String(e)));
  }, []);

  return (
    <div className="hv-tab">
      {/* chain health banner */}
      <div className="hv-chain-banner" data-intact={chain.intact ? 'true' : undefined}>
        <span className="hv-chain-icon">{chain.intact ? '✓' : '✕'}</span>
        <div className="hv-chain-text">
          <strong>审计链{chain.intact ? '完整' : '断裂'}</strong>
          <span>GOLD 签名 {chain.gold_signed ? '正常' : '异常'} · 最新 anchor {chain.latest_anchor}</span>
        </div>
        {!USE_MOCK && <span style={{ marginLeft: 'auto', fontSize: 10, color: 'var(--muted-foreground)' }}>live ·{chain.intact ? ' intact' : ' broken'}</span>}
      </div>

      {err && <div className="hv-honest-note" style={{ borderColor: 'var(--destructive)', color: 'var(--destructive)' }}>⚠ API error: {err}</div>}

      {/* 三策略状态卡 */}
      <div className="hv-section-title">策略状态{USE_MOCK ? ' (mock)' : ' (live)'}</div>
      <div className="hv-grid-3">
        {strategies.map(s => (
          <div key={s.strategy_id} className="hv-strat-card">
            <div className="hv-strat-card__head">
              <span className="hv-strat-name">{s.name}</span>
              <span className="hv-mode-badge" style={{ color: MODE_COLOR[s.mode] }}>{s.mode}</span>
            </div>
            <div className="hv-strat-card__row">
              <ORegimeBadge regime={s.regime} />
              <OGateBadge verdict={s.gate.verdict} dsr={s.gate.dsr} pbo={s.gate.pbo} compact />
            </div>
            <div className="hv-strat-card__meta">
              <span>持仓: {s.position}</span>
              <span>Paper 信号: {s.signals_today}</span>
            </div>
          </div>
        ))}
      </div>

      {/* paper account */}
      <div className="hv-section-title">Paper 账户 (OKX Demo)</div>
      <div className="hv-grid-3">
        <div className="hv-metric-card">
          <span className="hv-metric-label">USDT 余额</span>
          <span className="hv-metric-val">${account.balance.toLocaleString(undefined, { maximumFractionDigits: 2 })}</span>
        </div>
        <div className="hv-metric-card">
          <span className="hv-metric-label">今日 P&L (gross)</span>
          <span className={`hv-metric-val ${account.pnl_today_gross >= 0 ? 'hv-pos' : 'hv-neg'}`}>
            {account.pnl_today_gross >= 0 ? '+' : ''}{account.pnl_today_gross.toFixed(2)}
          </span>
        </div>
        <div className="hv-metric-card">
          <span className="hv-metric-label">今日 P&L (net)</span>
          <span className={`hv-metric-val ${account.pnl_today_net >= 0 ? 'hv-pos' : 'hv-neg'}`}>
            {account.pnl_today_net >= 0 ? '+' : ''}{account.pnl_today_net.toFixed(2)}
          </span>
          <span className="hv-metric-note">net &lt; gross (手续费/滑点)</span>
        </div>
      </div>

      {/* 诚实提示 */}
      <div className="hv-honest-note">
        ⚠️ 三策略 backtest gate 均为 FAIL（DSR/PBO 不达标）。Paper 短期 P&L 含运气成分，不代表策略可上 live。
      </div>
    </div>
  );
}
