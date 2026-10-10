// supabase/functions/admin-auth/index.ts
// 2HEART FINAL ADMIN SECURE 2FA
// Layer 2 of 3-Layer Security: V3 TOTP (RFC 6238) pure crypto.subtle HMAC-SHA1, no libraries.
// Actions: setup-2fa | verify-2fa | verify
// Audit log writes are immutable (DB has no UPDATE/DELETE policies).
// Deploy: supabase functions deploy admin-auth --no-verify-jwt
// Env: SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY, ADMIN_PASSWORD, ADMIN_EMAILS, ADMIN_2FA_SECRET

import { createClient } from 'https://esm.sh/@supabase/supabase-js@2';

const SUPABASE_URL = Deno.env.get('SUPABASE_URL')!;
const SERVICE_ROLE = Deno.env.get('SUPABASE_SERVICE_ROLE_KEY')!;
const ADMIN_PASSWORD = Deno.env.get('ADMIN_PASSWORD') ?? '';
const ADMIN_EMAILS = (Deno.env.get('ADMIN_EMAILS') ?? '').split(',').map((s) => s.trim().toLowerCase()).filter(Boolean);
const TOTP_SECRET = (Deno.env.get('ADMIN_2FA_SECRET') ?? 'JBSWY3DPEHPK3PXP').replace(/\s/g, '').toUpperCase();

const cors = {
  'Access-Control-Allow-Origin': '*',
  'Access-Control-Allow-Headers': 'authorization, x-client-info, apikey, content-type',
  'Access-Control-Allow-Methods': 'POST, OPTIONS',
};

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { ...cors, 'Content-Type': 'application/json', 'Cache-Control': 'no-store' },
  });
}

async function audit(action: string, email: string, ok: boolean, meta: Record<string, unknown> = {}) {
  const supabase = createClient(SUPABASE_URL, SERVICE_ROLE);
  await supabase.from('admin_audit_log').insert({ action, actor: email, ok, meta, at: new Date().toISOString() });
}

// ---------- Base32 ----------
const B32 = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ234567';
function base32Decode(s: string): Uint8Array {
  let bits = 0, value = 0, out: number[] = [];
  for (const c of s) {
    const idx = B32.indexOf(c);
    if (idx === -1) continue;
    value = (value << 5) | idx; bits += 5;
    if (bits >= 8) { bits -= 8; out.push((value >>> bits) & 0xff); }
  }
  return new Uint8Array(out);
}
function base32Encode(buf: Uint8Array): string {
  let bits = 0, value = 0, out = '';
  for (const b of buf) {
    value = (value << 8) | b; bits += 8;
    while (bits >= 5) { bits -= 5; out += B32[(value >>> bits) & 31]; }
  }
  if (bits > 0) out += B32[(value << (5 - bits)) & 31];
  return out;
}
function randomBase32(bytes = 20): string {
  return base32Encode(crypto.getRandomValues(new Uint8Array(bytes)));
}

// ---------- TOTP (RFC 6238) ----------
async function hotp(key: Uint8Array, counter: number): Promise<number> {
  const msg = new DataView(new ArrayBuffer(8));
  msg.setUint32(0, Math.floor(counter / 2 ** 32));
  msg.setUint32(4, counter >>> 0);
  const cryptoKey = await crypto.subtle.importKey('raw', key, { name: 'HMAC', hash: 'SHA-1' }, false, ['sign']);
  const sig = new Uint8Array(await crypto.subtle.sign('HMAC', cryptoKey, msg.buffer));
  const offset = sig[sig.length - 1] & 0x0f;
  const bin = ((sig[offset] & 0x7f) << 24) | (sig[offset + 1] << 16) | (sig[offset + 2] << 8) | sig[offset + 3];
  return bin % 1_000_000;
}
async function verifyTotp(secret: string, token: string, window = 1, step = 30): Promise<boolean> {
  const counter = Math.floor(Date.now() / 1000 / step);
  const clean = token.replace(/\D/g, '');
  for (let i = -window; i <= window; i++) {
    const expected = (await hotp(base32Decode(secret), counter + i)).toString().padStart(6, '0');
    if (expected === clean) return true;
  }
  return false;
}
function otpauthUri(secret: string, email: string, issuer = '2HEART'): string {
  const label = `${encodeURIComponent(issuer)}:${encodeURIComponent(email)}`;
  return `otpauth://totp/${label}?secret=${secret}&issuer=${encodeURIComponent(issuer)}&algorithm=SHA1&digits=6&period=30`;
}

// ---------- Handler ----------
Deno.serve(async (req) => {
  if (req.method === 'OPTIONS') return new Response('ok', { headers: cors });
  if (req.method !== 'POST') return json({ error: 'method_not_allowed' }, 405);

  let body: Record<string, unknown>;
  try { body = await req.json(); } catch { return json({ error: 'invalid_json' }, 400); }

  const action = String(body.action ?? '');
  const email = String(body.email ?? '').toLowerCase();
  const password = String(body.password ?? '');
  const token = String(body.token ?? '');

  const allowed = ADMIN_EMAILS.length === 0 || ADMIN_EMAILS.includes(email);

  try {
    if (action === 'setup-2fa') {
      if (!allowed || password !== ADMIN_PASSWORD) { await audit('setup-2fa', email, false); return json({ error: 'unauthorized' }, 401); }
      const secret = randomBase32();
      await audit('setup-2fa', email, true);
      return json({ secret, otpauth: otpauthUri(secret, email) });
    }

    if (action === 'verify-2fa' || action === 'verify') {
      if (!allowed || password !== ADMIN_PASSWORD) { await audit(action, email, false); return json({ error: 'unauthorized' }, 401); }
      const ok = await verifyTotp(TOTP_SECRET, token);
      await audit(action, email, ok);
      return ok ? json({ ok: true }) : json({ error: 'bad_token' }, 401);
    }

    return json({ error: 'unknown_action' }, 400);
  } catch (e) {
    return json({ error: 'server_error', detail: String(e) }, 500);
  }
});
