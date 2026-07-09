/**
 * ConfigureTab — 四策略实盘调参(真实 /config GET + PUT,改 YAML 的 `live` 块)
 * `live` 块是 paper 节点真正读取的参数;改完"保存并重启"即生效。
 * 下方 indicators/signal_logic 是研究/回测配置,不影响在跑的实盘策略(诚实标注)。
 */
'use client';

import { useEffect, useState } from 'react';
import { EmptyState, Skeleton, StaleBanner } from '../EmptyState';
import { helivexApi, ensembleApi } from '@/lib/api-client';
import { useApi } from '@/lib/use-api';
import type { StrategyState, EngineWeightsResp, ConsensusConfigResp } from '@/types/api';

type Cfg = Record<string, unknown> & {
  description?: string; timeframe?: string; instruments?: string[];
  live?: Record<string, number | boolean>;
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
  // helixa 移植策略 — trend_follower
  donchian_period:  { label: 'Donchian 周期 (bars)', step: 1, min: 2 },
  adx_period:       { label: 'ADX 周期', step: 1, min: 2 },
  adx_entry:        { label: 'ADX 入场门 (≥)', step: 1, min: 0 },
  adx_exit:         { label: 'ADX 出场门 (<)', step: 1, min: 0 },
  chandelier_period:{ label: 'Chandelier 周期 (bars)', step: 1, min: 2 },
  chandelier_mult:  { label: 'Chandelier ATR 乘数', step: 0.1, min: 0 },
  max_holding_days: { label: '最长持仓 (日)', step: 1, min: 1 },
  // scalper_v2
  bb_period:          { label: 'Bollinger 周期', step: 1, min: 2 },
  bb_k:               { label: 'Bollinger K (σ)', step: 0.1, min: 0 },
  rsi_period:         { label: 'RSI 周期', step: 1, min: 2 },
  adx_enter_breakout: { label: 'ADX 进突破模式 (≥)', step: 1, min: 0 },
  adx_exit_breakout:  { label: 'ADX 退突破模式 (<)', step: 1, min: 0 },
  cooldown_bars:      { label: '模式切换冷却 (bars)', step: 1, min: 0 },
  trailing_atr_mult:  { label: '突破移动止损 ATR 乘数', step: 0.1, min: 0 },
  breakout_exit_adx:  { label: '突破持仓 ADX 平仓 (<)', step: 1, min: 0 },
  max_holding_bars:   { label: '最长持仓 (bars)', step: 1, min: 1 },
  // futures-signal-engine
  breakout_period: { label: '突破窗口 (bars)', step: 1, min: 2 },
  vol_ma_period:   { label: '量能均线周期', step: 1, min: 2 },
  vol_surge_mult:  { label: '量能激增倍数 (×)', step: 0.1, min: 0 },
};

// live params that are booleans → rendered as a toggle instead of a number input
const LIVE_BOOL_META: Record<string, { label: string; onText: string; offText: string }> = {
  trade_enabled: { label: '交易开关', onText: '● paper 交易(下单)', offText: '○ observe(只记录)' },
};

export function ConfigureTab() {
  const { data: strategies, loading, error } = useApi<StrategyState[]>(() => helivexApi.strategies(), [], undefined, 'cfg-strats');
  const [sel, setSel] = useState<string | null>(null);
  const id = sel ?? strategies?.[0]?.strategy_id ?? null;
  const cfg = useApi<Cfg>(() => helivexApi.getConfig(id!) as Promise<Cfg>, [id], undefined, id ? `cfg:${id}` : undefined);

  const [draft, setDraft] = useState<Cfg | null>(null);
  const [saving, setSaving] = useState(false);
  const [restarting, setRestarting] = useState(false);
  const [restartConfirm, setRestartConfirm] = useState(false);
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);

  useEffect(() => {
    setDraft(cfg.data ? structuredClone(cfg.data) : null);
    setMsg(null);
    setRestartConfirm(false);
  }, [cfg.data]);

  if (loading && !strategies) return <div className="hv-tab"><Skeleton /></div>;
  if (error && !strategies) return <div className="hv-tab"><EmptyState text="网关连接失败" sub={error} /></div>;
  const list = strategies ?? [];
  if (list.length === 0) return <div className="hv-tab"><EmptyState text="暂无策略" /></div>;

  const setLive = (key: string, value: number | boolean) =>
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
    setRestartConfirm(false);
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
            <>
              {/* 布尔开关(如 trade_enabled:observe ↔ paper 交易)*/}
              {liveKeys.filter(k => typeof draft.live![k] === 'boolean').map(k => {
                const bm = LIVE_BOOL_META[k] ?? { label: k, onText: '开', offText: '关' };
                const on = Boolean(draft.live![k]);
                return (
                  <div key={k} className="hv-metric-card" style={{ gap: 6, flexDirection: 'row', alignItems: 'center', justifyContent: 'space-between' }}>
                    <span className="hv-metric-label">{bm.label}</span>
                    <button type="button" className="hv-strat-tab" data-active={on ? 'true' : undefined}
                      aria-pressed={on} onClick={() => setLive(k, !on)}
                      style={{ color: on ? 'var(--success,#3fb950)' : 'var(--muted-foreground)' }}>
                      {on ? bm.onText : bm.offText}
                    </button>
                  </div>
                );
              })}
              {/* 数值参数 */}
              <div className="hv-grid-3">
                {liveKeys.filter(k => typeof draft.live![k] !== 'boolean').map(k => {
                  const m = LIVE_META[k] ?? { label: k, step: 1 };
                  return (
                    <div key={k} className="hv-metric-card" style={{ gap: 6 }}>
                      <span className="hv-metric-label">{m.label}</span>
                      <input className="hv-param-input" type="number" step={m.step} min={m.min}
                        aria-label={m.label}
                        value={draft.live![k] as number}
                        onChange={e => setLive(k, Number(e.target.value))} />
                    </div>
                  );
                })}
              </div>
            </>
          )}

          <div className="hv-cfg-actions">
            <button className="hv-run-gate" onClick={save} disabled={saving || restarting || !dirty}>
              {saving ? '保存中…' : dirty ? '仅保存 (写 YAML)' : '无改动'}
            </button>
            {!restartConfirm ? (
              <button className="hv-btn-recover" onClick={() => setRestartConfirm(true)} disabled={saving || restarting || !dirty}>
                {restarting ? '重启节点中…' : '保存并重启节点生效'}
              </button>
            ) : (
              <span className="hv-kill-confirm">
                <span>确定重启 paper 节点?短暂停机、重连 OKX、平掉未保护持仓。</span>
                <span className="hv-kill-actions">
                  <button className="hv-kill-cancel" onClick={() => setRestartConfirm(false)}>取消</button>
                  <button className="hv-kill-confirm-btn" disabled={saving || restarting} onClick={saveAndRestart}>确认保存并重启</button>
                </span>
              </span>
            )}
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

      <ConsensusTuner />
    </div>
  );
}

/**
 * ConsensusTuner — 共识层在线调参(补齐 G,helixa /strategy 页等价物,更强)。
 * 全局(非 per-strategy):共识执行阈值 + 每引擎 base 权重。observe-only:只改"若执行
 * 需多强共识/各引擎多大话语权"的判据,adapter 下一轮(~2min)读库生效,永不下单。
 */
function ConsensusTuner() {
  const cfg = useApi<ConsensusConfigResp>(() => ensembleApi.consensusConfig(), [], 30000, 'cons-cfg');
  const w = useApi<EngineWeightsResp>(() => ensembleApi.weights(), [], 30000, 'cons-w');

  const [thr, setThr] = useState<number | null>(null);
  const [wDraft, setWDraft] = useState<Record<string, number>>({});
  const [saving, setSaving] = useState(false);
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);

  useEffect(() => { if (cfg.data) setThr(cfg.data.base_threshold); setMsg(null); }, [cfg.data]);
  useEffect(() => {
    if (w.data) setWDraft(Object.fromEntries(w.data.weights.map(x => [x.engine, x.base_weight])));
  }, [w.data]);

  if ((cfg.loading && !cfg.data) || (w.loading && !w.data)) return <div style={{ marginTop: 24 }}><Skeleton /></div>;
  const engines = w.data?.weights ?? [];
  const thrDirty = thr != null && cfg.data != null && thr !== cfg.data.base_threshold;
  const wChanged = engines.filter(e => wDraft[e.engine] != null && wDraft[e.engine] !== e.base_weight);
  const dirty = thrDirty || wChanged.length > 0;

  const save = async () => {
    setSaving(true); setMsg(null);
    try {
      if (thrDirty && thr != null) await ensembleApi.putConsensusConfig(thr);
      if (wChanged.length > 0)
        await ensembleApi.putEngineWeights(wChanged.map(e => ({ engine: e.engine, base_weight: wDraft[e.engine] })));
      setMsg({ ok: true, text: '已保存 — 共识 adapter 下一轮(≤2min)生效' });
      cfg.refetch?.(); w.refetch?.();
    } catch (e) {
      setMsg({ ok: false, text: `保存失败:${String((e as Error)?.message ?? e)}` });
    } finally { setSaving(false); }
  };

  return (
    <div style={{ marginTop: 28 }}>
      <div className="hv-section-title">共识层在线调参(observe · 全局,不分策略)</div>
      <div className="hv-grid-3">
        <div className="hv-metric-card" style={{ gap: 6 }}>
          <span className="hv-metric-label">共识执行阈值 base_threshold (0.1–0.9)</span>
          <input className="hv-param-input" type="number" step={0.01} min={0.1} max={0.9}
            aria-label="共识执行阈值"
            value={thr ?? ''} onChange={e => setThr(Number(e.target.value))} />
        </div>
        {engines.map(e => (
          <div key={e.engine} className="hv-metric-card" style={{ gap: 6 }}>
            <span className="hv-metric-label">
              {e.engine} base 权重 (0–5) · dyn {e.dyn_weight.toFixed(2)} · acc {e.accuracy != null ? (e.accuracy * 100).toFixed(0) + '%' : '—'}
            </span>
            <input className="hv-param-input" type="number" step={0.1} min={0} max={5}
              aria-label={`${e.engine} base 权重`}
              value={wDraft[e.engine] ?? ''} onChange={ev => setWDraft(d => ({ ...d, [e.engine]: Number(ev.target.value) }))} />
          </div>
        ))}
      </div>
      <div className="hv-cfg-actions">
        <button className="hv-run-gate" onClick={save} disabled={saving || !dirty}>
          {saving ? '保存中…' : dirty ? '保存共识调参' : '无改动'}
        </button>
        {msg && <span className="hv-gate-reason" style={{ color: msg.ok ? 'var(--success,#3fb950)' : 'var(--destructive)' }}>{msg.text}</span>}
      </div>
      <div className="hv-honest-note">
        阈值 = 「共识分需多强才算可执行」;引擎 base 权重 = 各引擎在共识里的话语权(有归因数据后
        EWMA 会在此基础上按胜率自适应 dyn 权重)。<strong>observe-only</strong>:只改判据,共识本身
        不下单;写库后由共识 adapter 下一轮读取生效。
      </div>
    </div>
  );
}
