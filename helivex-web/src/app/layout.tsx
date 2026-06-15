import './globals.css';
import type { Metadata } from 'next';
export const metadata: Metadata = { title: 'helivex', description: '量化执行系统' };
export default function RootLayout({ children }: { children: React.ReactNode }) {
  return <html lang="zh" data-theme="terminal-dark"><body>{children}</body></html>;
}
