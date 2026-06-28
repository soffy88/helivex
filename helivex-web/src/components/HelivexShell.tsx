'use client';

import { useState } from 'react';
import { OverviewTab } from './tabs/OverviewTab';
import { ConfigureTab } from './tabs/ConfigureTab';
import { BacktestTab, ExecutionsTab, PnLTab, AuditTab } from './tabs/OtherTabs';
import { PortfolioTab } from './tabs/PortfolioTab';
import { RiskTab, MicrostructureTab } from './tabs/RiskMicroTabs';
import { TabErrorBoundary } from './TabErrorBoundary';

const TABS = [
  { id: 'overview',   label: 'Overview' },
  { id: 'portfolio',  label: 'Portfolio' },
  { id: 'risk',       label: 'Risk' },
  { id: 'micro',      label: 'Microstructure' },
  { id: 'configure',  label: 'Configure' },
  { id: 'backtest',   label: 'Backtest' },
  { id: 'executions', label: 'Executions' },
  { id: 'pnl',        label: 'P&L' },
  { id: 'audit',      label: 'Audit' },
] as const;

export function HelivexShell() {
  const [tab, setTab] = useState('overview');

  const renderTab = () => {
    switch (tab) {
      case 'overview':   return <OverviewTab />;
      case 'portfolio':  return <PortfolioTab />;
      case 'risk':       return <RiskTab />;
      case 'micro':      return <MicrostructureTab />;
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
              onClick={() => setTab(t.id)}>{t.label}</button>
          ))}
        </nav>
        <span className="hv-mode-global">paper mode</span>
      </header>
      <main className="hv-main">
        <TabErrorBoundary tabName={tab} key={tab}>
          {renderTab()}
        </TabErrorBoundary>
      </main>
    </div>
  );
}
