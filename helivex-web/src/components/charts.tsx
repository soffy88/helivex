/**
 * charts — zero-dependency inline SVG charts (no chart lib).
 * Sparkline (line), Underwater (drawdown area), DivergingBars (signed metric).
 */
'use client';

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
  const W = 600;
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
        <polygon points={area} fill="color-mix(in oklch, var(--destructive) 18%, transparent)" />
        <polyline points={line} fill="none" stroke="var(--destructive)" strokeWidth="1.5" />
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
        const w = (Math.abs(v) / mag) * 50;
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
