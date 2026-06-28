/**
 * ConfigureTab — 四策略实盘调参(真实 /config GET + PUT,改 YAML 的 `live` 块)
 * `live` 块是 paper 节点真正读取的参数;改完"保存并重启"即生效。
 * 下方 indicators/signal_logic 是研究/回测配置,不影响在跑的实盘策略(诚实标注)。
 */
'use client';

import { useEffect, useState } from 'react';
import { EmptyState, Skeleton, StaleBanner } from '../EmptyState';
import { helivexApi } from '@/lib/api-client';
import { useApi } from '@/lib/use-api';
import type { StrategyState } from '@/types/api';

type Cfg = Record<string, unknown> & {
  description?: string; timeframe?: string; instruments?: string[];
  live?: Record<string, number>;
  indicators?: Record<string, Record<string, unknown>>;
  signal_logic?: Record<string, unknown>; risk?: Record<string, unknown>;
};

// friendly labels + step for the tunable live params
const LIVE_META: Record<string, { label: string; step: number; min?: number }> = {
  n_enter: { label: '入场周期 (Donchian 上轨 bars)', step: 1, min: 1 },
  n_exit:  { label: '出场周期 (Donchian 下轨 bars)', step: 1, min: 1 },
  vwap_n:  { label: 'VWAP 窗口 (bars)', step: 1, min: 1 },
  z_thr:   { label: 'z-score 阈值 (σ)', step: 0.1, min: 0 },
  hold:    { label: '持仓 bars (时间止损)', step: 1, min: 1 },
  bear_ma: { label: '熊市过滤 MA (0=关)', step: 1, min: 0 },
  qty_usd: { label: '每单名义 (USD)', step: 10, min: 0 },
};

export function ConfigureTab() {
  const { data: strategies, loading, error } = useApi<StrategyState[]>(() => helivexApi.strategies(), [], undefined, 'cfg-strats');
  const [sel, setSel] = useState<string | null>(null);
  const id = sel ?? strategies?.[0]?.strategy_id ?? null;
  const cfg = useApi<Cfg>(() => helivexApi.getConfig(id!) as Promise<Cfg>, [id], undefined, id ? `cfg:${id}` : undefined);

  const [draft, setDraft] = useState<Cfg | null>(null);
  const [saving, setSaving] = useState(false);
  const [restarting, setRestarting] = useState(false);
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);

  useEffect(() => {
    setDraft(cfg.data ? structuredClone(cfg.data) : null);
    setMsg(null);
  }, [cfg.data]);

  if (loading && !strategies) return <div className="hv-tab"><Skeleton /></div>;
  if (error && !strategies) return <div className="hv-tab"><EmptyState text="网关连接失败" sub={error} /></div>;
  const list = strategies ?? [];
  if (list.length === 0) return <div className="hv-tab"><EmptyState text="暂无策略" /></div>;

  const setLive = (key: string, value: number) =>
    setDraft(d => d ? { ...d, live: { ...(d.live ?? {}), [key]: value } } : d);

  const dirty = draft && cfg.data && JSON.stringify(draft) !== JSON.stringify(cfg.data);

  const save = async (): Promise<boolean> => {
    if (!id || !draft) return false;
    setSaving(true); setMsg(null);
    try {
      const r = await helivexApi.putConfig(id, draft);
      setMsg({ ok: true, text: `已保存到 ${r.path}` });
      return true;
    } catch (e) {
      setMsg({ ok: false, text: `保存失败:${String((e as Error)?.message ?? e)}` });
      return false;
    } finally { setSaving(false); }
  };

  const saveAndRestart = async () => {
    const ok = await save();
    if (!ok) return;
    setRestarting(true);
    try {
      const r = await helivexApi.restartPaper();
      setMsg(r.ok ? { ok: true, text: r.message ?? 'paper 节点已重启,新参数生效' }
                  : { ok: false, text: `重启失败:${r.reason ?? '未知'}` });
    } catch (e) {
      setMsg({ ok: false, text: `重启失败:${String((e as Error)?.message ?? e)}` });
    } finally { setRestarting(false); }
  };

  const liveKeys = draft?.live ? Object.keys(draft.live) : [];

  return (
    <div className="hv-tab">
      {(cfg.stale || (error && strategies)) && <StaleBanner error={cfg.error ?? error ?? ''} />}
      <div className="hv-strat-tabs">
        {list.map(s => (
          <button key={s.strategy_id} className="hv-strat-tab"
            data-active={(id === s.strategy_id) ? 'true' : undefined}
            onClick={() => setSel(s.strategy_id)}>{s.name}</button>
        ))}
      </div>

      {cfg.loading && !draft ? <Skeleton /> : cfg.error && !draft ? <EmptyState text="配置加载失败" sub={cfg.error} /> : draft && (
        <>
          <div className="hv-honest-note">{draft.description ?? ''}（{draft.timeframe ?? ''} · {(draft.instruments ?? []).join(', ')}）</div>

          {/* ── 实盘参数:节点真正读取的 ── */}
          <div className="hv-section-title">实盘参数 — 直接影响在跑的策略</div>
          {liveKeys.length === 0 ? (
            <EmptyState text="该策略无实盘参数(live 块)" sub="paper/node.py 未声明可调 live 参数" />
          ) : (
            <div className="hv-grid-3">
              {liveKeys.map(k => {
                const m = LIVE_META[k] ?? { label: k, step: 1 };
                return (
                  <div key={k} className="hv-metric-card" style={{ gap: 6 }}>
                    <span className="hv-metric-label">{m.label}</span>
                    <input className="hv-param-input" type="number" step={m.step} min={m.min}
                      value={draft.live![k]}
                      onChange={e => setLive(k, Number(e.target.value))} />
                  </div>
                );
              })}
            </div>
          )}

          <div className="hv-cfg-actions">
            <button className="hv-run-gate" onClick={save} disabled={saving || restarting || !dirty}>
              {saving ? '保存中…' : dirty ? '仅保存 (写 YAML)' : '无改动'}
            </button>
            <button className="hv-btn-recover" onClick={saveAndRestart} disabled={saving || restarting || !dirty}>
              {restarting ? '重启节点中…' : '保存并重启节点生效'}
            </button>
            {msg && <span className="hv-gate-reason" style={{ color: msg.ok ? 'var(--success,#3fb950)' : 'var(--destructive)' }}>{msg.text}</span>}
          </div>
          <div className="hv-honest-note">
            改 live 参数 → 写回 YAML → 节点重启时重新读取生效。「保存并重启」会重启 paper 节点
            (短暂停机、重连 OKX、平掉未保护持仓)。下方 indicators/signal_logic 为研究/回测配置,
            <strong> 不影响在跑的实盘策略</strong>。
          </div>

          {/* ── 研究配置(只读展示,不影响实盘)── */}
          {draft.indicators && Object.keys(draft.indicators).length > 0 && (
            <>
              <div className="hv-section-title">研究/回测配置(不影响实盘)</div>
              <div className="hv-grid-indicators">
                {Object.entries(draft.indicators).map(([name, params]) => (
                  <div key={name} className="hv-metric-card" style={{ alignItems: 'stretch', gap: 6 }}>
                    <span className="hv-strat-name">{name}{(params as { enabled?: boolean }).enabled === false ? ' (off)' : ''}</span>
                    {Object.entries(params).filter(([k]) => k !== 'enabled').map(([k, v]) => (
                      <div key={k} className="hv-micro-row"><span>{k}</span><span className="hv-num">{String(v)}</span></div>
                    ))}
                  </div>
                ))}
              </div>
            </>
          )}
        </>
      )}
    </div>
  );
}
