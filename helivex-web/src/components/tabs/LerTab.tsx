/**
 * LerTab — LER(爆仓衰竭回归,HELIVEX-IMPL_SPEC-LER-001)数据积累进度
 * Phase R 后数据源改 OKX(见 docs/HELIVEX-IMPL_SPEC-LER-001.md §3/§12),纯前向采集,
 * 尚在积累阶段,无回测。只展示原始覆盖,不假装已有可用信号样本——诚实空状态原则同 R16。
 */
'use client';

import { EmptyState, Skeleton, StaleBanner } from '../EmptyState';
import { lerApi } from '@/lib/api-client';
import { useApi } from '@/lib/use-api';
import type { LerCoverage, LerSourceCoverage } from '@/types/api';

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

export function LerTab() {
  const { data, loading, error, stale } = useApi<LerCoverage>(
    () => lerApi.coverage(), [], 30000, 'ler',
  );
  if (loading && !data) return <div className="hv-tab"><Skeleton /></div>;
  if (error && !data) return <div className="hv-tab"><EmptyState text="网关连接失败" sub={error} /></div>;
  const cov = data as LerCoverage;

  return (
    <div className="hv-tab">
      {stale && <StaleBanner error={error!} />}
      <div className="hv-section-title">
        LER 数据积累进度(HELIVEX-IMPL_SPEC-LER-001,venue={cov.venue}) · as of {new Date(cov.as_of).toLocaleString()}
      </div>

      {(['liquidations', 'ohlcv_1m', 'funding', 'oi'] as const).map(k => (
        <SourceTable key={k} name={k} rows={cov.sources[k]} />
      ))}

      <div className="hv-honest-note">
        ⚠️ 研究项目,尚在数据积累阶段:V3 判据要求每配置 ≥{cov.v3_threshold.required_n_trades_per_config} 笔交易
        (共 {cov.v3_threshold.n_configs} 个配置)。{cov.v3_threshold.note}
      </div>
    </div>
  );
}
