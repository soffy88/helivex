/**
 * TabErrorBoundary — Tab 级错误边界(§1.4)
 * 单个 Tab 崩 → 只显示"加载失败 + 重试",不整页白屏。
 */
'use client';

import { Component, type ReactNode } from 'react';

interface Props { children: ReactNode; tabName?: string; }
interface State { hasError: boolean; }

export class TabErrorBoundary extends Component<Props, State> {
  constructor(props: Props) {
    super(props);
    this.state = { hasError: false };
  }

  static getDerivedStateFromError(): State {
    return { hasError: true };
  }

  componentDidCatch(error: unknown) {
    // 可上报;不 throw,隔离崩溃
    console.error('[TabErrorBoundary]', this.props.tabName, error);
  }

  reset = () => this.setState({ hasError: false });

  render() {
    if (this.state.hasError) {
      return (
        <div className="hv-tab-error">
          <div className="hv-tab-error__text">此模块加载失败</div>
          <div className="hv-tab-error__sub">数据可能异常,其他模块不受影响。</div>
          <button className="hv-tab-error__retry" onClick={this.reset}>重试</button>
        </div>
      );
    }
    return this.props.children;
  }
}
