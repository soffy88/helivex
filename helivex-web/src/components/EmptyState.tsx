/**
 * EmptyState — 诚实空状态(§5 helivex 灵魂)
 * 绝不用 mock/backtest 数据填充冒充。
 */
export function EmptyState({ text, sub }: { text: string; sub?: string }) {
  return (
    <div className="hv-empty">
      <div className="hv-empty__text">{text}</div>
      {sub && <div className="hv-empty__sub">{sub}</div>}
    </div>
  );
}

/** Stale banner — shown when data is retained but the latest refresh failed.
 * Keeps the populated view alive instead of blanking it on a transient blip. */
export function StaleBanner({ error }: { error: string }) {
  return (
    <div className="hv-stale" role="status">
      <span className="hv-stale-dot" /> 重连中 — 显示最后已知数据 <span className="hv-stale-err">({error})</span>
    </div>
  );
}

/** Loading skeleton — shimmer placeholder instead of a blank/text flash. */
export function Skeleton() {
  return (
    <div className="hv-skel" aria-busy="true">
      <div className="hv-skel-bar w40" />
      <div className="hv-skel-row">
        <div className="hv-skel-block" /><div className="hv-skel-block" /><div className="hv-skel-block" />
      </div>
      <div className="hv-skel-bar w60" />
      <div className="hv-skel-bar" />
    </div>
  );
}
