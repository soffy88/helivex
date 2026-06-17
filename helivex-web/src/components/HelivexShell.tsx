'use client';

import { useState, useEffect } from 'react';
import { OverviewTab } from './tabs/OverviewTab';
import { ConfigureTab } from './tabs/ConfigureTab';
import { StrategiesTab, BacktestTab, ExecutionsTab, PnLTab, AuditTab } from './tabs/OtherTabs';
import { PortfolioTab } from './tabs/PortfolioTab';
import { StrategyDetail } from './StrategyDetail';
import { helivexApi } from '@/lib/api-client';
import { MOCK_STRATEGIES } from '@/lib/mock-data';
import type { StrategyState } from '@/types/api';

const TABS = [
  { id: 'overview',   label: 'Overview' },
  { id: 'strategies', label: 'Strategies' },
  { id: 'portfolio',  label: 'Portfolio' },
  { id: 'configure',  label: 'Configure' },
  { id: 'backtest',   label: 'Backtest' },
  { id: 'executions', label: 'Executions' },
  { id: 'pnl',        label: 'P&L' },
  { id: 'audit',      label: 'Audit' },
] as const;

export function HelivexShell() {
  const [tab, setTab] = useState('overview');
  const [drillId, setDrillId] = useState<string | null>(null);
  const [strategies, setStrategies] = useState<StrategyState[]>(MOCK_STRATEGIES);

  useEffect(() => {
    helivexApi.strategies().then(setStrategies).catch(console.error);
  }, []);

  const drilledStrategy = drillId ? strategies.find(s => s.strategy_id === drillId) : null;

  const renderTab = () => {
    if (tab === 'strategies' && drilledStrategy) {
      return <StrategyDetail strategy={drilledStrategy} onBack={() => setDrillId(null)} />;
    }
    switch (tab) {
      case 'overview':   return <OverviewTab />;
      case 'strategies': return <StrategiesTab strategies={strategies} onDrill={setDrillId} />;
      case 'portfolio':  return <PortfolioTab />;
      case 'configure':  return <ConfigureTab />;
      case 'backtest':   return <BacktestTab />;
      case 'executions': return <ExecutionsTab />;
      case 'pnl':        return <PnLTab />;
      case 'audit':      return <AuditTab />;
      default:           return <OverviewTab />;
    }
  };

  return (
    <div className="hv-shell">
      <header className="hv-header">
        <span className="hv-logo">helivex</span>
        <nav className="hv-nav">
          {TABS.map(t => (
            <button key={t.id} className="hv-nav-item"
              data-active={tab === t.id ? 'true' : undefined}
              onClick={() => { setTab(t.id); if (t.id !== 'strategies') setDrillId(null); }}>{t.label}</button>
          ))}
        </nav>
        <span className="hv-mode-global">paper mode</span>
      </header>
      <main className="hv-main">
        {renderTab()}
      </main>
    </div>
  );
}
