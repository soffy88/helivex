'use client';

import { useState } from 'react';
import { OverviewTab } from './tabs/OverviewTab';
import { ConfigureTab } from './tabs/ConfigureTab';
import { StrategiesTab, BacktestTab, ExecutionsTab, PnLTab, AuditTab } from './tabs/OtherTabs';
import { PortfolioTab } from './tabs/PortfolioTab';
import { StrategyDetail } from './StrategyDetail';
import { TabErrorBoundary } from './TabErrorBoundary';
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
  const [drilled, setDrilled] = useState<StrategyState | null>(null);

  const renderTab = () => {
    if (tab === 'strategies' && drilled) {
      return <StrategyDetail strategy={drilled} onBack={() => setDrilled(null)} />;
    }
    switch (tab) {
      case 'overview':   return <OverviewTab />;
      case 'strategies': return <StrategiesTab onDrill={setDrilled} />;
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
              onClick={() => { setTab(t.id); if (t.id !== 'strategies') setDrilled(null); }}>{t.label}</button>
          ))}
        </nav>
        <span className="hv-mode-global">paper mode</span>
      </header>
      <main className="hv-main">
        <TabErrorBoundary tabName={tab} key={tab + (drilled?.strategy_id ?? '')}>
          {renderTab()}
        </TabErrorBoundary>
      </main>
    </div>
  );
}
