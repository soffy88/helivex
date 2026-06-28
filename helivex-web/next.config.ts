import type { NextConfig } from 'next';

// Proxy the gateway through the Next origin (/gw/*) so the browser only ever talks
// to this server — works identically via localhost, the WSL IP, or a public tunnel,
// and keeps the gateway (:8765) off the public surface. The server-side hop resolves
// localhost reliably no matter where the browser is.
const GATEWAY_ORIGIN = process.env.GATEWAY_ORIGIN ?? 'http://127.0.0.1:8765';

const nextConfig: NextConfig = {
  reactStrictMode: true,
  transpilePackages: ['@helios/blocks', '@helios/oui'],
  async rewrites() {
    return [{ source: '/gw/:path*', destination: `${GATEWAY_ORIGIN}/:path*` }];
  },
};

export default nextConfig;
