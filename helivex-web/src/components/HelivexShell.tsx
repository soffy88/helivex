'use client';

import { useEffect, useState } from 'react';
import { usePathname, useRouter, useSearchParams } from 'next/navigation';
import { HomeTab } from './tabs/HomeTab';
import { ConfigureTab } from './tabs/ConfigureTab';
import { ExecVerifyTab } from './tabs/OtherTabs';
import { PortfolioTab } from './tabs/PortfolioTab';
import { RiskTab, MicrostructureTab } from './tabs/RiskMicroTabs';
import { LerTab } from './tabs/LerTab';
import { EnsembleTab } from './tabs/EnsembleTab';
import { TabErrorBoundary } from './TabErrorBoundary';

// IA 重构 12→8:Overview 并入 首页;P&L 并入 Portfolio;Backtest+Executions→执行与验证;
// Audit 并入 Ensemble。每个显示模块只归属一个 tab,消除重复。
const TABS = [
  { id: 'home',       label: '首页' },
  { id: 'portfolio',  label: 'Portfolio' },
  { id: 'ensemble',   label: 'Ensemble' },
  { id: 'risk',       label: 'Risk' },
  { id: 'micro',      label: 'Microstructure' },
  { id: 'execverify', label: '执行与验证' },
  { id: 'ler',        label: 'LER' },
  { id: 'configure',  label: 'Configure' },
] as const;

// 旧 tab id → 新归属(老书签/深链不 404,重定向到合并后的 tab)
const TAB_ALIAS: Record<string, string> = {
  overview: 'home', pnl: 'portfolio', audit: 'ensemble',
  backtest: 'execverify', executions: 'execverify',
};

const TAB_IDS = TABS.map(t => t.id) as readonly string[];

/** 头部终端状态:UTC 实时时钟(每秒)+ 网关连接健康点(30s 轮询 /gw/risk/status)。 */
function HeaderStatus() {
  const [now, setNow] = useState<string>('');
  const [ok, setOk] = useState(true);
  useEffect(() => {
    const fmt = () => new Date().toISOString().slice(11, 19) + ' UTC';
    setNow(fmt());
    const t = setInterval(() => setNow(fmt()), 1000);
    const ping = async () => {
      try { setOk((await fetch('/gw/risk/status', { cache: 'no-store' })).ok); }
      catch { setOk(false); }
    };
    ping();
    const p = setInterval(ping, 30000);
    return () => { clearInterval(t); clearInterval(p); };
  }, []);
  return (
    <>
      <span className="hv-conn" data-ok={ok ? 'true' : 'false'} title={ok ? '网关连接正常' : '网关不可达'}>
        <span className="hv-conn__dot" />{ok ? 'LIVE' : 'OFFLINE'}
      </span>
      <span className="hv-clock" suppressHydrationWarning>{now}</span>
    </>
  );
}

export function HelivexShell() {
  const router = useRouter();
  const pathname = usePathname();
  const params = useSearchParams();
  const raw = params.get('tab');
  const resolved = raw ? (TAB_ALIAS[raw] ?? raw) : null;
  const tab = resolved && TAB_IDS.includes(resolved) ? resolved : 'home';

  // tab change → push (back/forward navigates view history); preserves other params
  const setTab = (id: string) => {
    const p = new URLSearchParams(params.toString());
    p.set('tab', id);
    router.push(`${pathname}?${p.toString()}`, { scroll: false });
  };

  const renderTab = () => {
    switch (tab) {
      case 'home':       return <HomeTab />;
      case 'portfolio':  return <PortfolioTab />;
      case 'ensemble':   return <EnsembleTab />;
      case 'risk':       return <RiskTab />;
      case 'micro':      return <MicrostructureTab />;
      case 'execverify': return <ExecVerifyTab />;
      case 'ler':        return <LerTab />;
      case 'configure':  return <ConfigureTab />;
      default:           return <HomeTab />;
    }
  };

  return (
    <div className="hv-shell">
      <a className="hv-skip" href={`#panel-${tab}`}>跳到主内容</a>
      <header className="hv-header">
        <span className="hv-logo">helivex</span>
        <nav className="hv-nav" role="tablist" aria-label="视图">
          {TABS.map(t => (
            <button key={t.id} className="hv-nav-item" role="tab"
              id={`tab-${t.id}`}
              aria-controls={`panel-${t.id}`}
              aria-selected={tab === t.id}
              data-active={tab === t.id ? 'true' : undefined}
              onClick={() => setTab(t.id)}>{t.label}</button>
          ))}
        </nav>
        <div className="hv-head-right">
          <HeaderStatus />
          <span className="hv-mode-global">paper mode</span>
        </div>
      </header>
      <main className="hv-main" role="tabpanel"
        id={`panel-${tab}`} aria-labelledby={`tab-${tab}`} tabIndex={0}>
        <TabErrorBoundary tabName={tab} key={tab}>
          {renderTab()}
        </TabErrorBoundary>
      </main>
    </div>
  );
}
