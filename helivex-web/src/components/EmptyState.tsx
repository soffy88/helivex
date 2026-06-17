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
