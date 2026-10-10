// middleware.ts  (2HEART FINAL ADMIN SECURE 2FA — Layer 1)
// Vercel Edge Middleware protecting /admin/:path* with HTTP Basic Auth
// plus an httpOnly signed session cookie (1h). Issues noindex + DENY headers.
//
// Env (Vercel project settings):
//   ADMIN_PASSWORD  - shared admin password for Basic Auth
//   ADMIN_EMAILS    - comma separated whitelist, e.g. "you@x.com,ops@y.com"
//
// Flow: Basic Auth over https -> if valid, set httpOnly `admin_session` cookie
// (HMAC-signed email|exp) and let the request through. Otherwise 401 with
// WWW-Authenticate so the browser prompts. /admin/2fa.html is reachable to
// perform the TOTP second factor via the admin-auth edge function.

import { NextRequest, NextResponse } from 'next/server';

export const config = {
  matcher: ['/admin/:path*'],
};

const TTL = 60 * 60; // 1 hour

async function hmac(payload: string, secret: string): Promise<string> {
  const key = await crypto.subtle.importKey(
    'raw',
    new TextEncoder().encode(secret),
    { name: 'HMAC', hash: 'SHA-256' },
    false,
    ['sign'],
  );
  const sig = await crypto.subtle.sign('HMAC', key, new TextEncoder().encode(payload));
  return Array.from(new Uint8Array(sig)).map((b) => b.toString(16).padStart(2, '0')).join('');
}

function secureHeaders(res: NextResponse): NextResponse {
  res.headers.set('X-Robots-Tag', 'noindex, nofollow');
  res.headers.set('X-Frame-Options', 'DENY');
  res.headers.set('Referrer-Policy', 'no-referrer');
  res.headers.set('Cache-Control', 'no-store, max-age=0, must-revalidate');
  res.headers.set('Content-Security-Policy', "frame-ancestors 'none'");
  return res;
}

function parseBasic(auth: string): { user: string; pass: string } | null {
  try {
    const [scheme, encoded] = auth.split(' ');
    if (scheme !== 'Basic' || !encoded) return null;
    const decoded = atob(encoded);
    const idx = decoded.indexOf(':');
    if (idx === -1) return null;
    return { user: decoded.slice(0, idx), pass: decoded.slice(idx + 1) };
  } catch {
    return null;
  }
}

function unauthorized(): NextResponse {
  const res = new NextResponse('Unauthorized', {
    status: 401,
    headers: { 'WWW-Authenticate': 'Basic realm="2HEART Admin", charset="UTF-8"' },
  });
  return secureHeaders(res);
}

export async function middleware(req: NextRequest) {
  const password = process.env.ADMIN_PASSWORD ?? '';
  const emails = (process.env.ADMIN_EMAILS ?? '').split(',').map((s) => s.trim().toLowerCase()).filter(Boolean);

  // Allow the 2FA page through without a session so users can complete factor 2.
  const is2faPage = req.nextUrl.pathname === '/admin/2fa.html' || req.nextUrl.pathname === '/admin/2fa';

  // Verify existing httpOnly session cookie.
  const cookie = req.cookies.get('admin_session')?.value;
  if (cookie) {
    const idx = cookie.lastIndexOf('.');
    if (idx > 0) {
      const payload = cookie.slice(0, idx);
      const sig = cookie.slice(idx + 1);
      const expect = await hmac(payload, password);
      const exp = Number(payload.split('|')[1] ?? 0);
      if (sig === expect && Date.now() / 1000 < exp) {
        return secureHeaders(NextResponse.next());
      }
    }
  }

  if (is2faPage) return secureHeaders(NextResponse.next());

  // HTTP Basic Auth challenge.
  const auth = req.headers.get('authorization');
  if (!auth) return unauthorized();
  const creds = parseBasic(auth);
  if (!creds) return unauthorized();

  const emailOk = emails.length === 0 || emails.includes(creds.user.toLowerCase());
  if (!emailOk || creds.pass !== password) return unauthorized();

  // Issue signed session cookie.
  const payload = `${creds.user.toLowerCase()}|${Math.floor(Date.now() / 1000) + TTL}`;
  const sig = await hmac(payload, password);
  const value = `${payload}.${sig}`;
  const res = secureHeaders(NextResponse.next());
  res.cookies.set('admin_session', value, {
    httpOnly: true,
    secure: true,
    sameSite: 'strict',
    path: '/',
    maxAge: TTL,
  });
  return res;
}
