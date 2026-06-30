import { NextRequest, NextResponse } from 'next/server';

/**
 * HTTP Basic Auth gate for the whole dashboard, including the /gw gateway proxy.
 * (Next 16 renamed the `middleware` file convention to `proxy`.)
 *
 * Enforced ONLY when both DASH_USER and DASH_PASS are set in the environment —
 * so local dev stays open, but any external exposure (tunnel/funnel) is password-
 * locked. This matters: the dashboard can trip the kill-switch and stop strategies,
 * and the gateway behind /gw has no auth of its own.
 */
export function proxy(req: NextRequest) {
  const user = process.env.DASH_USER;
  const pass = process.env.DASH_PASS;
  if (!user || !pass) return NextResponse.next(); // auth disabled when unset

  // Gate ALL non-local traffic — covers any public exposure (Cloudflare tunnel
  // btc.uex.hk, Tailscale Funnel, WSL/LAN IP). Direct localhost stays open. A
  // public client's Host is the public domain (it can't forge localhost through
  // the tunnel). Set DASH_FORCE_AUTH=1 to require auth everywhere.
  const host = (req.headers.get('host') ?? '').toLowerCase();
  const isLocal = host.startsWith('localhost') || host.startsWith('127.0.0.1') || host.startsWith('[::1]');
  if (isLocal && process.env.DASH_FORCE_AUTH !== '1') return NextResponse.next();

  const header = req.headers.get('authorization') ?? '';
  const expected = 'Basic ' + btoa(`${user}:${pass}`);
  if (header !== expected) {
    return new NextResponse('Authentication required', {
      status: 401,
      headers: { 'WWW-Authenticate': 'Basic realm="helivex", charset="UTF-8"' },
    });
  }
  return NextResponse.next();
}

// Protect everything except Next internals/assets (no data there; the sensitive
// data path is /gw, which IS gated). Excluding all of /_next avoids auth breaking
// asset/HMR loads over the funnel.
export const config = {
  matcher: ['/((?!_next/|favicon.ico).*)'],
};
