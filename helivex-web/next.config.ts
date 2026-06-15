import type { NextConfig } from 'next';
const nextConfig: NextConfig = { reactStrictMode: true, transpilePackages: ['@helios/blocks', '@helios/oui'] };
export default nextConfig;
