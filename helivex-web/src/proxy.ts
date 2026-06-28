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

// Protect everything except Next's static asset bundles (no data there).
export const config = {
  matcher: ['/((?!_next/static|_next/image|favicon.ico).*)'],
};
