/**
 * PortfolioTab — 全局组合视图(§2)
 * 合并资金曲线 + 策略相关性矩阵 + 总览 + 全局 kill switch
 */
'use client';

import { useState } from 'react';
import { EmptyState } from '../EmptyState';
import { MOCK_PORTFOLIO_SUMMARY, MOCK_CORRELATION } from '@/lib/mock-data';

export function PortfolioTab() {
  const sum = MOCK_PORTFOLIO_SUMMARY;
  const corr = MOCK_CORRELATION;
  const [killConfirm, setKillConfirm] = useState(false);

  // 相关性 → 颜色(低相关绿,高相关红)
  const corrColor = (v: number) => {
    if (v >= 0.99) return 'var(--muted)';
    const abs = Math.abs(v);
    return abs < 0.3 ? 'color-mix(in oklch, var(--success, oklch(0.62 0.18 145)) 30%, transparent)'
      : abs < 0.6 ? 'color-mix(in oklch, oklch(0.70 0.15 80) 30%, transparent)'
      : 'color-mix(in oklch, var(--destructive) 30%, transparent)';
  };

  return (
    <div className="hv-tab">
      {/* 总览 */}
      <div className="hv-section-title">组合总览</div>
      <div className="hv-grid-4">
        <div className="hv-metric-card"><span className="hv-metric-label">总持仓</span><span className="hv-metric-val">{sum.total_positions}</span></div>
        <div className="hv-metric-card"><span className="hv-metric-label">总未实现盈亏</span><span className="hv-metric-val">${sum.total_unrealized_pnl}</span></div>
        <div className="hv-metric-card"><span className="hv-metric-label">保证金占用</span><span className="hv-metric-val">${sum.margin_used}</span></div>
        <div className="hv-metric-card"><span className="hv-metric-label">可用资金</span><span className="hv-metric-val">${sum.available.toLocaleString()}</span></div>
      </div>

      {/* 合并资金曲线 */}
      <div className="hv-section-title">合并资金曲线</div>
      <EmptyState text="净值 = 初始 15000" sub="三策略均无成交,等首笔 fill" />

      {/* 相关性矩阵 */}
      <div className="hv-section-title">策略相关性(低相关 = 分散好)</div>
      <div className="hv-corr-matrix">
        <table className="hv-table">
          <thead>
            <tr>
              <th></th>
              {corr.strategies.map(s => <th key={s} className="hv-num">{s.split('_')[0]}</th>)}
            </tr>
          </thead>
          <tbody>
            {corr.matrix.map((row, i) => (
              <tr key={i}>
                <td>{corr.strategies[i]!.split('_')[0]}</td>
                {row.map((v, j) => (
                  <td key={j} className="hv-num" style={{ background: corrColor(v), textAlign: 'center' }}>
                    {v.toFixed(2)}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <div className="hv-honest-note">低相关性利于组合分散。边界策略组合可能整体过 gate(R4)。</div>

      {/* kill switch */}
      <div className="hv-section-title">风险控制</div>
      {!killConfirm ? (
        <button className="hv-kill-btn" onClick={() => setKillConfirm(true)}>⏹ 一键停所有策略</button>
      ) : (
        <div className="hv-kill-confirm">
          <span>确定停止所有策略?这会平掉所有 paper 持仓。</span>
          <div className="hv-kill-actions">
            <button className="hv-kill-cancel" onClick={() => setKillConfirm(false)}>取消</button>
            <button className="hv-kill-confirm-btn" onClick={() => setKillConfirm(false)}>确认停止</button>
          </div>
        </div>
      )}
    </div>
  );
}
