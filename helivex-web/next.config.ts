import type { NextConfig } from 'next';

// The gateway proxy now lives in src/app/gw/[...path]/route.ts (a server-side
// route handler) so it can inject the X-Helivex-Token header — a static rewrite
// cannot. The browser still only ever talks to this Next origin (/gw/*); the
// gateway (:8765) stays off the public surface and bound to 127.0.0.1.

const nextConfig: NextConfig = {
  reactStrictMode: true,
  transpilePackages: ['@helios/blocks', '@helios/oui'],
};

export default nextConfig;
