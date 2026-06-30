import { NextRequest, NextResponse } from 'next/server';

/**
 * Server-side proxy to the api-gateway. Replaces the old next.config.ts rewrite
 * so we can inject the shared gateway token (X-Helivex-Token) here, on the server —
 * the browser never holds it. The gateway binds 127.0.0.1 and (when HELIVEX_GW_TOKEN
 * is set) requires this header on mutating routes. Basic Auth (src/proxy.ts) still
 * gates the browser-facing surface in front of this handler.
 */
const GATEWAY_ORIGIN = process.env.GATEWAY_ORIGIN ?? 'http://127.0.0.1:8765';
const GW_TOKEN = process.env.HELIVEX_GW_TOKEN ?? '';

async function handler(
  req: NextRequest,
  ctx: { params: Promise<{ path: string[] }> },
): Promise<NextResponse> {
  const { path } = await ctx.params;
  const url = `${GATEWAY_ORIGIN}/${(path ?? []).join('/')}${req.nextUrl.search}`;

  const headers = new Headers();
  const ct = req.headers.get('content-type');
  if (ct) headers.set('content-type', ct);
  if (GW_TOKEN) headers.set('x-helivex-token', GW_TOKEN);

  const init: RequestInit = { method: req.method, headers };
  if (req.method !== 'GET' && req.method !== 'HEAD') {
    init.body = await req.text();
  }

  try {
    const res = await fetch(url, init);
    const body = await res.text();
    return new NextResponse(body, {
      status: res.status,
      headers: { 'content-type': res.headers.get('content-type') ?? 'application/json' },
    });
  } catch (e) {
    return NextResponse.json(
      { detail: 'gateway unreachable', error: String(e) },
      { status: 502 },
    );
  }
}

export const GET = handler;
export const POST = handler;
export const PUT = handler;
export const DELETE = handler;
export const dynamic = 'force-dynamic';
