/**
 * OverviewTab — 切真: 从 gateway 取数据
 */
'use client';

import { useEffect, useState } from 'react';
import { OGateBadge, ORegimeBadge } from '@helios/blocks';
import { helivexApi } from '@/lib/api-client';
import type { StrategyState } from '@/types/api';

const MODE_COLOR: Record<string, string> = {
  shadow: 'var(--muted-foreground)', paper: 'oklch(0.62 0.16 200)', live: 'oklch(0.60 0.22 25)',
};

interface ChainDisplay { intact: boolean; gold_signed: boolean; latest_anchor: string }
interface AccountDisplay { balance: number; positions: number; pnl_today_gross: number; pnl_today_net: number }

export function OverviewTab() {
  const [strategies, setStrategies] = useState<StrategyState[]>([]);
  const [chain, setChain] = useState<ChainDisplay | null>(null);
  const [account, setAccount] = useState<AccountDisplay | null>(null);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    Promise.all([
      helivexApi.strategies().then(setStrategies),
      helivexApi.chainHealth().then((r: any) =>
        setChain({ intact: r.ok, gold_signed: r.n_gold > 0, latest_anchor: `records:${r.n_total} gold:${r.n_gold}` })
      ),
      helivexApi.account().then(setAccount),
    ]).catch(e => setErr(String(e)));
  }, []);

  return (
    <div className="hv-tab">
      {err && <div className="hv-honest-note" style={{ color: 'var(--destructive)' }}>⚠ API error: {err}</div>}

      {chain && (
        <div className="hv-chain-banner" data-intact={chain.intact ? 'true' : undefined}>
          <span className="hv-chain-icon">{chain.intact ? '✓' : '✕'}</span>
          <div className="hv-chain-text">
            <strong>审计链{chain.intact ? '完整' : '断裂'}</strong>
            <span>GOLD 签名 {chain.gold_signed ? '正常' : '异常'} · {chain.latest_anchor}</span>
          </div>
        </div>
      )}

      <div className="hv-section-title">策略状态</div>
      {strategies.length === 0 && <div className="hv-honest-note">加载策略中…</div>}
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
              <span>信号数: {s.signals_today}</span>
            </div>
          </div>
        ))}
      </div>

      <div className="hv-section-title">Paper 账户</div>
      {account && (
        <div className="hv-grid-3">
          <div className="hv-metric-card">
            <span className="hv-metric-label">余额</span>
            <span className="hv-metric-val">${account.balance.toLocaleString()}</span>
          </div>
          <div className="hv-metric-card">
            <span className="hv-metric-label">今日 P&L (gross)</span>
            <span className="hv-metric-val hv-pos">{account.pnl_today_gross >= 0 ? '+' : ''}${account.pnl_today_gross}</span>
          </div>
          <div className="hv-metric-card">
            <span className="hv-metric-label">今日 P&L (net)</span>
            <span className="hv-metric-val hv-pos">{account.pnl_today_net >= 0 ? '+' : ''}${account.pnl_today_net}</span>
            <span className="hv-metric-note">net &lt; gross(手续费/滑点)</span>
          </div>
        </div>
      )}

      <div className="hv-honest-note">
        ⚠️ Paper 短期 P&L ≠ 策略有效(含运气成分),需配合执行真实度 + 足够长样本判断。
      </div>
    </div>
  );
}
