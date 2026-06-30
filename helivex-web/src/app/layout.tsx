import './globals.css';
import type { Metadata, Viewport } from 'next';
export const metadata: Metadata = { title: 'helivex', description: '量化执行系统' };
export const viewport: Viewport = {
  width: 'device-width',
  initialScale: 1,
  themeColor: '#0a0a0a',
};
export default function RootLayout({ children }: { children: React.ReactNode }) {
  return <html lang="zh" data-theme="terminal-dark"><body>{children}</body></html>;
}
