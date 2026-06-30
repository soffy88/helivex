'use client';

import { usePathname, useRouter, useSearchParams } from 'next/navigation';
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

const TAB_IDS = TABS.map(t => t.id) as readonly string[];

export function HelivexShell() {
  const router = useRouter();
  const pathname = usePathname();
  const params = useSearchParams();
  const raw = params.get('tab');
  const tab = raw && TAB_IDS.includes(raw) ? raw : 'overview';

  // tab change → push (back/forward navigates view history); preserves other params
  const setTab = (id: string) => {
    const p = new URLSearchParams(params.toString());
    p.set('tab', id);
    router.push(`${pathname}?${p.toString()}`, { scroll: false });
  };

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
        <nav className="hv-nav" role="tablist" aria-label="视图">
          {TABS.map(t => (
            <button key={t.id} className="hv-nav-item" role="tab"
              aria-selected={tab === t.id}
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
