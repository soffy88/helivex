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
