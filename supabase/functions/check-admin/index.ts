// supabase/functions/check-admin/index.ts
// Layer 2b: verifies the httpOnly admin cookie handed out by middleware.ts.
// Returns { admin: true/false }. Deploy: supabase functions deploy check-admin --no-verify-jwt
import { createClient } from 'https://esm.sh/@supabase/supabase-js@2';

const SUPABASE_URL = Deno.env.get('SUPABASE_URL')!;
const SERVICE_ROLE = Deno.env.get('SUPABASE_SERVICE_ROLE_KEY')!;
const COOKIE_SECRET = Deno.env.get('ADMIN_PASSWORD') ?? 'changeme';
const TTL = 60 * 60; // 1 hour, matches middleware

const cors = { 'Access-Control-Allow-Origin': '*', 'Access-Control-Allow-Headers': 'authorization, x-client-info, apikey, content-type' };
const json = (b: unknown, s = 200) => new Response(JSON.stringify(b), { status: s, headers: { ...cors, 'Content-Type': 'application/json', 'Cache-Control': 'no-store' } });

async function sign(payload: string): Promise<string> {
  const enc = new TextEncoder();
  const key = await crypto.subtle.importKey('raw', enc.encode(COOKIE_SECRET), { name: 'HMAC', hash: 'SHA-256' }, false, ['sign']);
  const sig = await crypto.subtle.sign('HMAC', key, enc.encode(payload));
  return Array.from(new Uint8Array(sig)).map((b) => b.toString(16).padStart(2, '0')).join('');
}

Deno.serve(async (req) => {
  if (req.method === 'OPTIONS') return new Response('ok', { headers: cors });
  const cookie = req.headers.get('cookie') ?? '';
  const m = cookie.match(/(?:^|;\s*)admin_session=([^;]+)/);
  if (!m) return json({ admin: false }, 401);
  const value = decodeURIComponent(m[1]);
  const idx = value.lastIndexOf('.');
  if (idx < 0) return json({ admin: false }, 401);
  const payload = value.slice(0, idx);
  const sig = value.slice(idx + 1);
  const expect = await sign(payload);
  if (sig !== expect) return json({ admin: false }, 401);
  const parts = payload.split('|');
  const exp = Number(parts[1] ?? 0);
  if (Date.now() / 1000 > exp) return json({ admin: false }, 401);
  // audit read (immutable insert)
  const supabase = createClient(SUPABASE_URL, SERVICE_ROLE);
  await supabase.from('admin_audit_log').insert({ action: 'check-admin', actor: parts[0] ?? '', ok: true, meta: {}, at: new Date().toISOString() });
  return json({ admin: true, email: parts[0] ?? '', expires_at: exp, ttl: TTL });
});
