/**
 * SafeBadges — Badge 健壮性 wrapper(§1.1 枚举 fallback)
 *
 * blocks 2.8.0 的 OGateBadge/ORegimeBadge 映射表无 fallback,
 * 未知 verdict(如后端新增 'no-go')→ undefined.color 崩。
 *
 * 此 wrapper 在 helivex 层归一化:
 * - blocks 认识的值(pass/fail/pending)→ 透传 OGateBadge
 * - no-go / 未知值 → helivex 自渲染(不进 blocks,避免崩)
 *
 * 注:理想方案是 blocks 层加 fallback(§5,多项目受益),
 * 但 blocks 源码当前不在沙箱,先在消费层止血。blocks 源码恢复后上提。
 */
'use client';

import { OGateBadge, ORegimeBadge } from '@helios/blocks';
import type { GateVerdict, Regime } from '@/types/api';

const BLOCKS_KNOWN_VERDICT = new Set(['pass', 'fail', 'pending']);
const BLOCKS_KNOWN_REGIME = new Set(['trend', 'chop', 'bear', 'bull']);

// no-go / 未知 verdict 的本地渲染
const VERDICT_FALLBACK: Record<string, { color: string; icon: string; label: string }> = {
  'no-go':   { color: 'oklch(0.70 0.15 65)',           icon: '⊘', label: 'NO-GO' },
  'unknown': { color: 'var(--muted-foreground, #888)', icon: '?', label: 'UNKNOWN' },
};

export function SafeGateBadge({
  verdict, dsr, pbo, reason, compact,
}: {
  verdict: GateVerdict | string | undefined | null;
  dsr?: number; pbo?: number; reason?: string; compact?: boolean;
}) {
  // normalize: real gate data is UPPERCASE (FAIL/PASS); blocks expects lowercase
  const v = String(verdict ?? 'unknown').toLowerCase();

  // blocks 认识 → 透传
  if (BLOCKS_KNOWN_VERDICT.has(v)) {
    return <OGateBadge verdict={v as 'pass'} dsr={dsr} pbo={pbo} reason={reason} compact={compact} />;
  }

  // no-go / 未知 → 本地安全渲染
  const m = VERDICT_FALLBACK[v] ?? VERDICT_FALLBACK['unknown']!;
  return (
    <span
      className="inline-flex items-center gap-1.5 rounded-md px-2 py-1 text-xs font-semibold tabular-nums"
      style={{
        color: m.color,
        background: `color-mix(in oklch, ${m.color} 14%, transparent)`,
        border: `1px solid color-mix(in oklch, ${m.color} 30%, transparent)`,
      }}
      title={reason}
    >
      <span aria-hidden>{m.icon}</span>
      <span>{m.label}</span>
      {!compact && dsr != null && <span className="opacity-80">DSR {dsr.toFixed(2)}</span>}
      {!compact && pbo != null && <span className="opacity-80">PBO {(pbo * 100).toFixed(0)}%</span>}
    </span>
  );
}

export function SafeRegimeBadge({
  regime, compact,
}: {
  regime: Regime | string | undefined | null;
  compact?: boolean;
}) {
  const r = String(regime ?? 'unknown').toLowerCase();
  if (BLOCKS_KNOWN_REGIME.has(r)) {
    return <ORegimeBadge regime={r as 'trend'} compact={compact} />;
  }
  // 未知 regime → 灰色 fallback
  return (
    <span className="inline-flex items-center gap-1 rounded px-2 py-0.5 text-xs font-semibold"
      style={{ color: 'var(--muted-foreground, #888)', background: 'color-mix(in oklch, var(--muted-foreground, #888) 14%, transparent)' }}>
      <span aria-hidden>?</span>{!compact && <span>{r.toUpperCase()}</span>}
    </span>
  );
}
