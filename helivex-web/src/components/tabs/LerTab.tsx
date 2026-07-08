/**
 * LerTab — LER(爆仓衰竭回归,HELIVEX-IMPL_SPEC-LER-001)研究状态:策略定义/参数 + 数据积累进度
 * Phase R 后数据源改 OKX(见 docs/HELIVEX-IMPL_SPEC-LER-001.md §3/§12),纯前向采集,
 * 尚在积累阶段,无回测、无 gate 判决。只展示原始事实,不假装已有可用信号样本或实盘状态
 * ——诚实空状态原则同 R16。
 *
 * 刻意不接入 /strategies、Configure tab 的实盘参数编辑与"保存并重启节点"流程:LER 是
 * 研究阶段的 spec 快照,不是可实盘/模拟盘下单的策略(spec §2 明确禁止),这里全程只读。
 */
'use client';

import { EmptyState, Skeleton, StaleBanner } from '../EmptyState';
import { lerApi } from '@/lib/api-client';
import { useApi } from '@/lib/use-api';
import type { LerCoverage, LerSourceCoverage, LerConfig } from '@/types/api';

const SOURCE_LABELS: Record<string, string> = {
  liquidations: '强平(md.liquidations)',
  ohlcv_1m: '1m OHLCV(md.ohlcv)',
  funding: 'Funding(md.funding_settled)',
  oi: 'OI(md.oi)',
};

function freshnessColor(mins: number | null): string {
  if (mins === null) return 'var(--muted-foreground)';
  if (mins > 30) return 'var(--destructive)';
  if (mins > 10) return 'oklch(0.70 0.15 80)';
  return 'var(--success, oklch(0.62 0.18 145))';
}

function SourceTable({ name, rows }: { name: string; rows: LerSourceCoverage[] }) {
  return (
    <div className="hv-ler-source">
      <div className="hv-section-title">{SOURCE_LABELS[name] ?? name}</div>
      {rows.length === 0 ? (
        <EmptyState text="暂无数据" sub="采集器可能还没写入,或该 symbol 尚无记录" />
      ) : (
        <table className="hv-table" aria-label={SOURCE_LABELS[name] ?? name}>
          <thead>
            <tr>
              <th>Symbol</th><th>行数</th><th>起点</th><th>止点</th><th>覆盖天数</th><th>新鲜度</th>
            </tr>
          </thead>
          <tbody>
            {rows.map(r => (
              <tr key={r.symbol}>
                <td>{r.symbol}</td>
                <td>{r.rows.toLocaleString()}</td>
                <td>{r.first_ts ? new Date(r.first_ts).toLocaleString() : '—'}</td>
                <td>{r.last_ts ? new Date(r.last_ts).toLocaleString() : '—'}</td>
                <td>{r.days_covered.toFixed(2)}</td>
                <td style={{ color: freshnessColor(r.freshness_minutes) }}>
                  {r.freshness_minutes === null ? '—' : `${r.freshness_minutes.toFixed(1)}min 前`}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}

/** 一组 key-value 参数,渲染成跟 ConfigureTab 研究配置同款的小卡片 */
function ParamCard({ title, params, badges }: { title: string; params: Record<string, unknown>; badges?: (k: string) => string | null }) {
  return (
    <div className="hv-metric-card" style={{ alignItems: 'stretch', gap: 6 }}>
      <span className="hv-strat-name">{title}</span>
      {Object.entries(params).map(([k, v]) => (
        <div key={k} className="hv-micro-row">
          <span>{k}{badges?.(k) ? <em style={{ marginLeft: 4, fontStyle: 'normal', fontSize: 'var(--text-xs)', color: 'var(--muted-foreground)' }}>[{badges(k)}]</em> : null}</span>
          <span className="hv-num">{typeof v === 'object' ? JSON.stringify(v) : String(v)}</span>
        </div>
      ))}
    </div>
  );
}

function ParamsSection({ cfg }: { cfg: LerConfig }) {
  const lockSet = new Set(cfg.locked_params.map(p => p.split('.').pop()));
  const changeSet = new Set(cfg.changeable_params.map(p => p.split('.').pop()));
  const badge = (k: string) => (lockSet.has(k) ? '锁定' : changeSet.has(k) ? '可改·须Wiki批准' : null);

  return (
    <>
      <div className="hv-section-title">策略定义 — {cfg.spec_ref} {cfg.spec_version}</div>
      <div className="hv-honest-note">
        <strong>{cfg.description}</strong> · 状态:<strong>{cfg.status}</strong>(非 backtest/paper/live,禁止实盘/模拟盘下单)
        · {cfg.timeframe} · {cfg.instruments.join(', ')}
      </div>

      <div className="hv-section-title">信号参数(预注册,进管线前锁定 —— spec §5)</div>
      <div className="hv-grid-indicators">
        <ParamCard title="触发 T1/T2" params={cfg.trigger} badges={badge} />
        <ParamCard title="衰竭确认 E1/E2" params={cfg.exhaustion} badges={badge} />
        <ParamCard title="Regime 过滤 F1/F2" params={cfg.regime_filters} badges={badge} />
        <ParamCard title="离场" params={cfg.exit} badges={badge} />
        <ParamCard title="仓位与风控" params={cfg.risk} badges={badge} />
        {Object.entries(cfg.execution).map(([name, params]) => (
          <ParamCard key={name} title={`执行 — ${name}`} params={params} badges={badge} />
        ))}
      </div>

      <div className="hv-section-title">预注册配置预算(§6,全集 = 8,不得追加)</div>
      <table className="hv-table" aria-label="配置预算">
        <thead><tr><th>ID</th><th>说明</th></tr></thead>
        <tbody>
          {cfg.configs.map(c => (<tr key={c.id}><td>{c.id}</td><td>{c.desc}</td></tr>))}
        </tbody>
      </table>

      <div className="hv-honest-note">
        Wiki 已批准变更(2026-07-08): {cfg.already_amended.join(', ')}。
        其余「可改」参数须走 Wiki 批准 + 重新预注册,不由本页直接编辑——LER 无实盘参数、
        无「保存并重启节点」流程,和另外四个已过 gate 的策略明确区分开。
      </div>
    </>
  );
}

export function LerTab() {
  const { data, loading, error, stale } = useApi(
    () => Promise.all([lerApi.coverage(), lerApi.config()]),
    [], 30000, 'ler',
  );
  if (loading && !data) return <div className="hv-tab"><Skeleton /></div>;
  if (error && !data) return <div className="hv-tab"><EmptyState text="网关连接失败" sub={error} /></div>;
  const [cov, cfg] = data as [LerCoverage, LerConfig];

  return (
    <div className="hv-tab">
      {stale && <StaleBanner error={error!} />}

      <ParamsSection cfg={cfg} />

      <div className="hv-section-title">
        数据积累进度(venue={cov.venue}) · as of {new Date(cov.as_of).toLocaleString()}
      </div>
      {(['liquidations', 'ohlcv_1m', 'funding', 'oi'] as const).map(k => (
        <SourceTable key={k} name={k} rows={cov.sources[k]} />
      ))}

      <div className="hv-honest-note">
        ⚠️ V3 判据要求每配置 ≥{cov.v3_threshold.required_n_trades_per_config} 笔交易
        (共 {cov.v3_threshold.n_configs} 个配置)。{cov.v3_threshold.note}
      </div>
    </div>
  );
}
