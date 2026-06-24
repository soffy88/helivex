/**
 * ConfigureTab — 策略配置编辑器(真实 /config GET + PUT,无 mock)
 * 编辑指标开关/参数 + signal_logic + risk,保存写回 YAML。
 * 注:gate 运行(注册 trial、跑数分钟)是研究/CLI 职责,不放 UI 按钮 —
 *     避免随手点击污染诚实账本;真实账本见 Backtest tab。
 */
'use client';

import { useEffect, useState } from 'react';
import { EmptyState, Skeleton } from '../EmptyState';
import { helivexApi } from '@/lib/api-client';
import { useApi } from '@/lib/use-api';
import type { StrategyState } from '@/types/api';

type Cfg = Record<string, unknown> & {
  description?: string; timeframe?: string; instruments?: string[];
  indicators?: Record<string, Record<string, unknown>>;
  signal_logic?: Record<string, unknown>; risk?: Record<string, unknown>;
};

export function ConfigureTab() {
  const { data: strategies, loading, error } = useApi<StrategyState[]>(() => helivexApi.strategies(), []);
  const [sel, setSel] = useState<string | null>(null);
  const id = sel ?? strategies?.[0]?.strategy_id ?? null;
  const cfg = useApi<Cfg>(() => helivexApi.getConfig(id!) as Promise<Cfg>, [id]);

  const [draft, setDraft] = useState<Cfg | null>(null);
  const [saving, setSaving] = useState(false);
  const [saveMsg, setSaveMsg] = useState<{ ok: boolean; text: string } | null>(null);

  useEffect(() => {
    setDraft(cfg.data ? structuredClone(cfg.data) : null);
    setSaveMsg(null);
  }, [cfg.data]);

  if (loading) return <div className="hv-tab"><Skeleton /></div>;
  if (error) return <div className="hv-tab"><EmptyState text="网关连接失败" sub={error} /></div>;
  const list = strategies ?? [];
  if (list.length === 0) return <div className="hv-tab"><EmptyState text="暂无策略" /></div>;

  const setIndParam = (ind: string, key: string, value: number | boolean) =>
    setDraft(d => d ? { ...d, indicators: { ...d.indicators, [ind]: { ...d.indicators![ind], [key]: value } } } : d);
  const setLogic = (key: string, value: unknown) =>
    setDraft(d => d ? { ...d, signal_logic: { ...d.signal_logic, [key]: value } } : d);
  const setRisk = (key: string, value: number) =>
    setDraft(d => d ? { ...d, risk: { ...d.risk, [key]: value } } : d);

  const save = async () => {
    if (!id || !draft) return;
    setSaving(true); setSaveMsg(null);
    try {
      const r = await helivexApi.putConfig(id, draft);
      setSaveMsg({ ok: true, text: `已保存到 ${r.path}` });
    } catch (e) {
      setSaveMsg({ ok: false, text: `保存失败:${String((e as Error)?.message ?? e)}` });
    } finally { setSaving(false); }
  };

  const dirty = draft && cfg.data && JSON.stringify(draft) !== JSON.stringify(cfg.data);

  return (
    <div className="hv-tab">
      <div className="hv-strat-tabs">
        {list.map(s => (
          <button key={s.strategy_id} className="hv-strat-tab"
            data-active={(id === s.strategy_id) ? 'true' : undefined}
            onClick={() => setSel(s.strategy_id)}>{s.name}</button>
        ))}
      </div>

      {cfg.loading ? <Skeleton /> : cfg.error ? <EmptyState text="配置加载失败" sub={cfg.error} /> : draft && (
        <>
          <div className="hv-honest-note">{draft.description ?? ''}（{draft.timeframe ?? ''} · {(draft.instruments ?? []).join(', ')}）</div>

          <div className="hv-section-title">指标配置</div>
          {!draft.indicators || Object.keys(draft.indicators).length === 0 ? (
            <EmptyState text="该策略无可调指标" />
          ) : (
            <div className="hv-grid-indicators">
              {Object.entries(draft.indicators).map(([name, params]) => (
                <div key={name} className="hv-metric-card" style={{ alignItems: 'stretch', gap: 8 }}>
                  <label className="hv-logic-ctrl" style={{ flexDirection: 'row', alignItems: 'center', justifyContent: 'space-between' }}>
                    <span className="hv-strat-name">{name}</span>
                    <input type="checkbox" checked={!!(params as { enabled?: boolean }).enabled}
                      onChange={e => setIndParam(name, 'enabled', e.target.checked)} />
                  </label>
                  {Object.entries(params).filter(([k, v]) => k !== 'enabled' && typeof v === 'number').map(([k, v]) => (
                    <label key={k} className="hv-logic-ctrl" style={{ flexDirection: 'row', alignItems: 'center', justifyContent: 'space-between' }}>
                      <span>{k}</span>
                      <input type="number" value={v as number} step="any" style={{ width: 90 }}
                        onChange={e => setIndParam(name, k, Number(e.target.value))} />
                    </label>
                  ))}
                </div>
              ))}
            </div>
          )}

          <div className="hv-section-title">Signal Logic / Risk</div>
          <div className="hv-logic-controls" style={{ flexWrap: 'wrap' }}>
            {'min_confluence' in (draft.signal_logic ?? {}) && (
              <label className="hv-logic-ctrl">min_confluence
                <input type="number" min={1} max={6} value={Number(draft.signal_logic!.min_confluence)}
                  onChange={e => setLogic('min_confluence', Number(e.target.value))} />
              </label>
            )}
            {'direction' in (draft.signal_logic ?? {}) && (
              <label className="hv-logic-ctrl">direction
                <select value={String(draft.signal_logic!.direction)} onChange={e => setLogic('direction', e.target.value)}>
                  <option value="both">both</option><option value="long">long</option><option value="short">short</option>
                </select>
              </label>
            )}
            {draft.risk && 'cost_bps' in draft.risk && (
              <label className="hv-logic-ctrl">cost_bps
                <input type="number" step="any" value={Number(draft.risk.cost_bps)}
                  onChange={e => setRisk('cost_bps', Number(e.target.value))} />
              </label>
            )}
          </div>

          <div className="hv-gate-section">
            <button className="hv-run-gate" onClick={save} disabled={saving || !dirty}>
              {saving ? '保存中…' : dirty ? '保存配置 (PUT)' : '无改动'}
            </button>
            {saveMsg && <div className="hv-gate-reason" style={{ color: saveMsg.ok ? 'var(--success,#3fb950)' : 'var(--destructive)' }}>{saveMsg.text}</div>}
            <div className="hv-honest-note">改配置写回 YAML,paper 节点下次重启生效。跑 gate(注册 trial)是研究/CLI 职责,见 Backtest tab 的真实账本。</div>
          </div>
        </>
      )}
    </div>
  );
}
