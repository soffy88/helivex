/**
 * charts — zero-dependency inline SVG charts (no chart lib).
 * EquityChart (terminal-grade equity curve), Sparkline (line),
 * Underwater (drawdown area), DivergingBars (signed metric).
 */
'use client';

import { useEffect, useId, useRef, useState } from 'react';

/** "nice" 轴刻度:1/2/2.5/5×10^k 步长,覆盖 [lo,hi],约 n 条 */
function niceTicks(lo: number, hi: number, n: number): number[] {
  const span = hi - lo;
  if (!(span > 0)) return [lo];
  const mag = 10 ** Math.floor(Math.log10(span / n));
  const step = [1, 2, 2.5, 5, 10].map(m => m * mag).find(s => span / s <= n + 0.5) ?? mag * 10;
  const out: number[] = [];
  for (let v = Math.ceil(lo / step) * step; v <= hi + step * 1e-6; v += step) out.push(v);
  return out;
}

export interface EquityChartPt { date: string; equity: number; drawdown?: number }

/**
 * EquityChart — 资金曲线(标杆样式:自适应量程 + 网格/轴标签 + 渐变面积 +
 * 起点基线 + 悬停十字线/tooltip)。量程按数据 min/max 自适应(带 8% padding),
 * 绝对美元净值直接可用,不会被 0 下限或归一化锚点压扁。
 */
export function EquityChart({ pts, h = 240, currency = '$' }: {
  pts: EquityChartPt[]; h?: number; currency?: string;
}) {
  const wrapRef = useRef<HTMLDivElement>(null);
  const [w, setW] = useState(640);
  const [hover, setHover] = useState<number | null>(null);
  const gid = useId();
  useEffect(() => {
    const el = wrapRef.current;
    if (!el) return;
    const ro = new ResizeObserver(es => {
      for (const e of es) setW(Math.max(320, Math.floor(e.contentRect.width)));
    });
    ro.observe(el);
    return () => ro.disconnect();
  }, []);
  if (pts.length < 2) return null;

  const PAD_L = 56, PAD_R = 14, PAD_T = 12, PAD_B = 22;
  const iw = w - PAD_L - PAD_R, ih = h - PAD_T - PAD_B;
  const vals = pts.map(p => p.equity);
  const rawLo = Math.min(...vals), rawHi = Math.max(...vals);
  const span = (rawHi - rawLo) || Math.abs(rawHi) * 0.001 || 1;
  const lo = rawLo - span * 0.08, hi = rawHi + span * 0.08;
  const x = (i: number) => PAD_L + (i / (pts.length - 1)) * iw;
  const y = (v: number) => PAD_T + (1 - (v - lo) / (hi - lo)) * ih;
  const up = vals[vals.length - 1] >= vals[0];
  const col = up ? 'var(--success, #3fb950)' : 'var(--destructive, #f85149)';
  const line = pts.map((p, i) => `${x(i).toFixed(1)},${y(p.equity).toFixed(1)}`).join(' ');
  const floorY = (PAD_T + ih).toFixed(1);
  const area = `${x(0).toFixed(1)},${floorY} ${line} ${x(pts.length - 1).toFixed(1)},${floorY}`;
  const ticks = niceTicks(lo, hi, 4);
  const spanMs = +new Date(pts[pts.length - 1].date) - +new Date(pts[0].date);
  const fmtX = (d: string) => {
    const t = new Date(d);
    return spanMs > 3 * 864e5
      ? `${t.getUTCMonth() + 1}/${t.getUTCDate()}`
      : `${String(t.getUTCHours()).padStart(2, '0')}:${String(t.getUTCMinutes()).padStart(2, '0')}`;
  };
  const fmtY = (v: number) => (v < 0 ? '-' : '') + currency + (Math.abs(v) >= 1000
    ? Math.abs(v).toLocaleString('en-US', { maximumFractionDigits: 0 })
    : Math.abs(v).toFixed(2));
  const xTickIdx = [...new Set([0, 0.33, 0.66, 1].map(f => Math.round(f * (pts.length - 1))))];
  const base = vals[0];
  const hv = hover != null ? pts[hover] : null;

  const onMove = (e: React.MouseEvent<SVGSVGElement>) => {
    const rect = e.currentTarget.getBoundingClientRect();
    const fx = ((e.clientX - rect.left) / rect.width) * w;
    const i = Math.round(((fx - PAD_L) / iw) * (pts.length - 1));
    setHover(Math.max(0, Math.min(pts.length - 1, i)));
  };

  return (
    <div ref={wrapRef} style={{ position: 'relative', width: '100%' }}>
      <svg width="100%" height={h} viewBox={`0 0 ${w} ${h}`}
        onMouseMove={onMove} onMouseLeave={() => setHover(null)}
        role="img" aria-label={`资金曲线,${pts.length} 个点,${fmtY(rawLo)}–${fmtY(rawHi)}`}>
        <defs>
          <linearGradient id={gid} x1="0" y1="0" x2="0" y2="1">
            <stop offset="0" stopColor={col} stopOpacity="0.22" />
            <stop offset="1" stopColor={col} stopOpacity="0" />
          </linearGradient>
        </defs>
        {ticks.map(t => (
          <g key={t}>
            <line x1={PAD_L} x2={w - PAD_R} y1={y(t)} y2={y(t)}
              stroke="var(--border, #30363d)" strokeWidth="1" strokeDasharray="1 4" />
            <text x={PAD_L - 8} y={y(t) + 3} textAnchor="end" fontSize="10"
              fontFamily="var(--font-mono)" fill="var(--muted-foreground, #8b949e)">{fmtY(t)}</text>
          </g>
        ))}
        {/* 起点基线 */}
        <line x1={PAD_L} x2={w - PAD_R} y1={y(base)} y2={y(base)}
          stroke="var(--muted-foreground, #8b949e)" strokeWidth="1" strokeDasharray="4 4" opacity="0.5" />
        <polygon points={area} fill={`url(#${gid})`} />
        <polyline points={line} fill="none" stroke={col} strokeWidth="1.5"
          strokeLinejoin="round" strokeLinecap="round" />
        {xTickIdx.map(i => (
          <text key={i} x={x(i)} y={h - 6} textAnchor={i === 0 ? 'start' : i === pts.length - 1 ? 'end' : 'middle'}
            fontSize="10" fontFamily="var(--font-mono)" fill="var(--muted-foreground, #8b949e)">
            {fmtX(pts[i].date)}
          </text>
        ))}
        {/* 末值点 */}
        <circle cx={x(pts.length - 1)} cy={y(vals[vals.length - 1])} r="2.5" fill={col} />
        {hv && hover != null && (
          <g>
            <line x1={x(hover)} x2={x(hover)} y1={PAD_T} y2={PAD_T + ih}
              stroke="var(--muted-foreground, #8b949e)" strokeWidth="1" strokeDasharray="2 3" opacity="0.7" />
            <circle cx={x(hover)} cy={y(hv.equity)} r="3" fill={col} stroke="var(--background, #0d1117)" strokeWidth="1.5" />
          </g>
        )}
      </svg>
      {hv && hover != null && (
        <div style={{
          position: 'absolute', pointerEvents: 'none',
          left: `${Math.min(92, Math.max(2, (x(hover) / w) * 100))}%`, top: 4,
          transform: x(hover) > w * 0.6 ? 'translateX(-105%)' : 'translateX(8px)',
          background: 'var(--popover, #161b22)', border: '1px solid var(--border, #30363d)',
          borderRadius: 4, padding: '4px 8px', fontSize: 11, fontFamily: 'var(--font-mono)',
          whiteSpace: 'nowrap', zIndex: 5,
        }}>
          <div style={{ color: 'var(--muted-foreground, #8b949e)' }}>{new Date(hv.date).toISOString().slice(0, 16).replace('T', ' ')} UTC</div>
          <div style={{ color: col }}>{fmtY(hv.equity)}
            {base !== 0 && (
              <span style={{ color: 'var(--muted-foreground, #8b949e)', marginLeft: 6 }}>
                {(((hv.equity - base) / base) * 100).toFixed(3)}%
              </span>
            )}
          </div>
          {hv.drawdown != null && hv.drawdown < 0 && (
            <div style={{ color: 'var(--destructive, #f85149)' }}>DD {(hv.drawdown * 100).toFixed(2)}%</div>
          )}
        </div>
      )}
    </div>
  );
}

type EqRange = '24h' | '7d' | 'all';
type EqMode = 'nav' | 'pnl';
const RANGE_MS: Record<Exclude<EqRange, 'all'>, number> = { '24h': 864e5, '7d': 7 * 864e5 };

/**
 * EquityPanel — 带控件的资金曲线面板(对齐 Hyperliquid 组合页):
 * 时间范围切换(24H/7D/ALL,按最后一个数据点回溯,不依赖本地时钟)+
 * 净值/PnL 双模式(PnL = 相对所选窗口起点的美元盈亏,起点=0)。
 */
export function EquityPanel({ pts, title, h = 250 }: {
  pts: EquityChartPt[]; title: string; h?: number;
}) {
  const [range, setRange] = useState<EqRange>('all');
  const [mode, setMode] = useState<EqMode>('nav');
  const shown = (() => {
    let sel = pts;
    if (range !== 'all' && pts.length > 0) {
      const cutoff = +new Date(pts[pts.length - 1].date) - RANGE_MS[range];
      sel = pts.filter(p => +new Date(p.date) >= cutoff);
    }
    if (mode === 'pnl' && sel.length > 0) {
      const b = sel[0].equity;
      sel = sel.map(p => ({ ...p, equity: p.equity - b }));
    }
    return sel;
  })();
  const pill = (active: boolean): React.CSSProperties => ({
    padding: '2px 10px', borderRadius: 6, fontSize: 11, cursor: 'pointer',
    fontFamily: 'var(--font-mono)', lineHeight: '18px',
    border: `1px solid ${active ? 'transparent' : 'var(--border, #30363d)'}`,
    background: active ? 'var(--primary)' : 'transparent',
    color: active ? 'var(--primary-foreground, #fff)' : 'var(--muted-foreground, #8b949e)',
  });
  const last = shown[shown.length - 1];
  const first = shown[0];
  const delta = last && first ? last.equity - (mode === 'pnl' ? 0 : first.equity) : 0;
  return (
    <div>
      <div className="hv-panel__head" style={{ marginBottom: 6 }}>
        <span className="hv-panel__title">{title}</span>
        <span style={{ display: 'flex', gap: 4, alignItems: 'center' }}>
          {last && (
            <span style={{
              fontFamily: 'var(--font-mono)', fontSize: 12, marginRight: 8,
              color: delta >= 0 ? 'var(--success, #3fb950)' : 'var(--destructive, #f85149)',
            }}>
              {mode === 'pnl'
                ? `${last.equity >= 0 ? '+' : '-'}$${Math.abs(last.equity).toFixed(2)}`
                : `$${last.equity.toLocaleString('en-US', { maximumFractionDigits: 2 })}`}
            </span>
          )}
          <button style={pill(mode === 'nav')} onClick={() => setMode('nav')}>净值</button>
          <button style={pill(mode === 'pnl')} onClick={() => setMode('pnl')}>PnL</button>
          <span style={{ width: 8 }} />
          {(['24h', '7d', 'all'] as const).map(r => (
            <button key={r} style={pill(range === r)} onClick={() => setRange(r)}>{r.toUpperCase()}</button>
          ))}
        </span>
      </div>
      {shown.length < 2
        ? <div className="hv-empty__sub" style={{ padding: '32px 0', textAlign: 'center' }}>该时间范围内成交点不足</div>
        : <EquityChart pts={shown} h={h} />}
    </div>
  );
}

/** tiny line sparkline */
export function Sparkline({ pts, color = 'var(--primary)', w = 120, h = 28 }: {
  pts: number[]; color?: string; w?: number; h?: number;
}) {
  if (pts.length < 2) return <span className="hv-spark-empty">—</span>;
  const min = Math.min(...pts), max = Math.max(...pts), rng = max - min || 1;
  const d = pts.map((p, i) => `${(i / (pts.length - 1)) * w},${h - ((p - min) / rng) * h}`).join(' ');
  const last = pts[pts.length - 1];
  return (
    <svg className="hv-spark" viewBox={`0 0 ${w} ${h}`} width={w} height={h} preserveAspectRatio="none"
      role="img" aria-label={`走势图,${pts.length} 个点,当前 ${last.toLocaleString(undefined, { maximumFractionDigits: 4 })},区间 ${min.toLocaleString(undefined, { maximumFractionDigits: 4 })}–${max.toLocaleString(undefined, { maximumFractionDigits: 4 })}`}>
      <polyline points={d} fill="none" stroke={color} strokeWidth="1.5" />
    </svg>
  );
}

/** drawdown underwater: area hanging down from 0 to each (negative) drawdown value */
export function Underwater({ pts, h = 80 }: { pts: number[]; h?: number }) {
  if (pts.length < 2) return null;
  const W = 1280; // 接近实际渲染宽度,避免 preserveAspectRatio=none 横向拉伸出粗锯齿
  // pts are fractional drawdowns (<= 0). worst = most negative.
  const worst = Math.min(...pts, 0);
  const scale = worst < 0 ? h / Math.abs(worst) : 0;
  const x = (i: number) => (i / (pts.length - 1)) * W;
  const y = (v: number) => Math.min(h, Math.abs(Math.min(0, v)) * scale);  // depth from top (0)
  const line = pts.map((v, i) => `${x(i)},${y(v)}`).join(' ');
  const area = `0,0 ${line} ${W},0`;
  const worstPct = (worst * 100).toFixed(1);
  return (
    <div className="hv-uw">
      <svg viewBox={`0 0 ${W} ${h}`} width="100%" height={h} preserveAspectRatio="none"
        role="img" aria-label={`回撤水下图,${pts.length} 个点,最深回撤 ${worstPct}%`}>
        <polygon points={area} fill="color-mix(in oklch, var(--destructive) 12%, transparent)" />
        <polyline points={line} fill="none" stroke="var(--destructive)" strokeWidth="1" strokeOpacity="0.85"
          strokeLinejoin="round" />
      </svg>
      <span className="hv-uw-label">最深回撤 {worstPct}%</span>
    </div>
  );
}

/** horizontal diverging bars from a center line — for signed metrics (e.g. DSR) */
export function DivergingBars({ items, unit = '' }: {
  items: { label: string; value: number | null; ok?: boolean }[]; unit?: string;
}) {
  const vals = items.map(i => i.value ?? 0);
  const mag = Math.max(0.01, ...vals.map(Math.abs));
  return (
    <div className="hv-dbars">
      {items.map((it, i) => {
        const v = it.value ?? 0;
        // sqrt 量程:单一 outlier 主导时(如一个策略亏损占 99%),线性刻度会把
        // 其余条压成 <1px 不可见;sqrt 保序但压缩支配度,小值仍可辨。
        const w = v === 0 ? 0 : Math.max(0.6, Math.sqrt(Math.abs(v) / mag) * 50);
        const pos = v >= 0;
        const color = it.ok === undefined
          ? (pos ? 'var(--success, oklch(0.62 0.18 145))' : 'var(--destructive)')
          : (it.ok ? 'var(--success, oklch(0.62 0.18 145))' : 'var(--destructive)');
        return (
          <div key={i} className="hv-dbar-row">
            <span className="hv-dbar-label">{it.label}</span>
            <div className="hv-dbar-track">
              <div className="hv-dbar-center" />
              <div className="hv-dbar-fill" style={{
                left: pos ? '50%' : `${50 - w}%`, width: `${w}%`, background: color,
              }} />
            </div>
            <span className="hv-dbar-val hv-num">{it.value === null ? '—' : `${v.toFixed(2)}${unit}`}</span>
          </div>
        );
      })}
    </div>
  );
}

/** 手写 SVG K 线蜡烛图 + 成交标记(无图表库)。 */
export interface Candle { ts: string; o: number; h: number; l: number; c: number; }
export interface CandleMarker { ts: string; side: string; price: number; strategy?: string; burst?: boolean; }
export function Candlestick({ candles, markers = [], w = 640, h = 260 }: {
  candles: Candle[]; markers?: CandleMarker[]; w?: number; h?: number;
}) {
  if (candles.length < 2) return <div className="hv-empty__sub">K 线数据不足</div>;
  const lows = candles.map(c => c.l), highs = candles.map(c => c.h);
  const min = Math.min(...lows), max = Math.max(...highs), rng = max - min || 1;
  const pad = 8, iw = w - pad * 2, ih = h - pad * 2;
  const n = candles.length, cw = iw / n;
  const y = (v: number) => pad + ih - ((v - min) / rng) * ih;
  const up = 'var(--success, oklch(0.62 0.18 145))', dn = 'var(--destructive)';
  const tsIndex = (ts: string) => {
    // nearest candle index by timestamp
    const t = new Date(ts).getTime();
    let best = 0, bd = Infinity;
    candles.forEach((c, i) => { const d = Math.abs(new Date(c.ts).getTime() - t); if (d < bd) { bd = d; best = i; } });
    return best;
  };
  return (
    <svg className="hv-candles" viewBox={`0 0 ${w} ${h}`} width="100%" height={h} preserveAspectRatio="none"
      role="img" aria-label={`K线图,${n} 根,区间 ${min.toFixed(2)}–${max.toFixed(2)}`}>
      {candles.map((c, i) => {
        const x = pad + i * cw + cw / 2;
        const col = c.c >= c.o ? up : dn;
        const bodyTop = y(Math.max(c.o, c.c)), bodyBot = y(Math.min(c.o, c.c));
        return (
          <g key={i}>
            <line x1={x} x2={x} y1={y(c.h)} y2={y(c.l)} stroke={col} strokeWidth="1" />
            <rect x={x - cw * 0.35} y={bodyTop} width={cw * 0.7} height={Math.max(1, bodyBot - bodyTop)} fill={col} />
          </g>
        );
      })}
      {markers.map((m, i) => {
        const idx = tsIndex(m.ts);
        const x = pad + idx * cw + cw / 2;
        const my = y(m.price);
        const buy = m.side === 'buy';
        // replay-burst fills get an amber warning dot instead of a directional triangle
        if (m.burst) return (
          <g key={'m' + i}>
            <circle cx={x} cy={my} r={3.5} fill="oklch(0.72 0.16 70)" stroke="var(--background)" strokeWidth="0.5" />
            <text x={x} y={my - 6} fontSize="8" textAnchor="middle" fill="oklch(0.72 0.16 70)">⚠</text>
          </g>
        );
        return (
          <polygon key={'m' + i}
            points={buy ? `${x},${my + 8} ${x - 4},${my + 14} ${x + 4},${my + 14}` : `${x},${my - 8} ${x - 4},${my - 14} ${x + 4},${my - 14}`}
            fill={buy ? up : dn} stroke="var(--background)" strokeWidth="0.5" />
        );
      })}
    </svg>
  );
}
