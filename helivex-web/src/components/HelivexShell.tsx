'use client';

import { useState } from 'react';
import { OverviewTab } from './tabs/OverviewTab';
import { ConfigureTab } from './tabs/ConfigureTab';
import { StrategiesTab, BacktestTab, ExecutionsTab, PnLTab, AuditTab } from './tabs/OtherTabs';

const TABS = [
  { id: 'overview',   label: 'Overview',   El: OverviewTab },
  { id: 'strategies', label: 'Strategies', El: StrategiesTab },
  { id: 'configure',  label: 'Configure',  El: ConfigureTab },
  { id: 'backtest',   label: 'Backtest',   El: BacktestTab },
  { id: 'executions', label: 'Executions', El: ExecutionsTab },
  { id: 'pnl',        label: 'P&L',        El: PnLTab },
  { id: 'audit',      label: 'Audit',      El: AuditTab },
] as const;

export function HelivexShell() {
  const [tab, setTab] = useState('overview');
  const Active = TABS.find(t => t.id === tab)?.El ?? OverviewTab;

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
        <Active />
      </main>
    </div>
  );
}
