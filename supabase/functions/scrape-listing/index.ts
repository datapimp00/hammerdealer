// supabase/functions/scrape-listing/index.ts
// Layer: on-demand single-listing scrape. Body { url, portal? } -> parses, upserts one row.
// Deploy: supabase functions deploy scrape-listing --no-verify-jwt
import { createClient } from 'https://esm.sh/@supabase/supabase-js@2';

const SUPABASE_URL = Deno.env.get('SUPABASE_URL')!;
const SERVICE_ROLE = Deno.env.get('SUPABASE_SERVICE_ROLE_KEY')!;
const PROXY = (Deno.env.get('PROXY_LIST') ?? '').split(',').filter(Boolean);

const cors = { 'Access-Control-Allow-Origin': '*', 'Access-Control-Allow-Headers': 'authorization, x-client-info, apikey, content-type' };
const json = (b: unknown, s = 200) => new Response(JSON.stringify(b), { status: s, headers: { ...cors, 'Content-Type': 'application/json' } });

async function fetchHtml(url: string): Promise<string | null> {
  try {
    const p = PROXY.length ? PROXY[Math.floor(Math.random() * PROXY.length)] : null;
    const headers: Record<string, string> = { 'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) 2HEARTBot/1.0' };
    if (p) headers['x-proxy-hint'] = p;
    const res = await fetch(url, { headers, redirect: 'follow' });
    return res.ok ? await res.text() : null;
  } catch { return null; }
}
function titleOf(html: string): string {
  const m = html.match(/<title>([^<]{4,120})<\/title>/i);
  return m ? m[1].trim() : 'Listing';
}
function priceOf(html: string): number {
  const m = html.match(/(\d{2,5})[\s.]?(\d{2})?\s*(?:€|EUR|Euro)/i);
  if (!m) return 0;
  return parseInt(m[1], 10);
}
function imageOf(html: string): string {
  const m = html.match(/<meta[^>]+property="og:image"[^>]+content="([^"]+)"/i) || html.match(/<img[^>]+src="(https?:\/\/[^"]+\.(?:jpg|jpeg|png|webp))"/i);
  return m ? m[1] : '';
}

Deno.serve(async (req) => {
  if (req.method === 'OPTIONS') return new Response('ok', { headers: cors });
  if (req.method !== 'POST') return json({ error: 'method_not_allowed' }, 405);
  let body: Record<string, unknown>;
  try { body = await req.json(); } catch { return json({ error: 'invalid_json' }, 400); }
  const url = String(body.url ?? '');
  const portal = String(body.portal ?? 'manual');
  if (!/^https?:\/\//.test(url)) return json({ error: 'bad_url' }, 400);

  const supabase = createClient(SUPABASE_URL, SERVICE_ROLE);
  const html = await fetchHtml(url);
  if (!html) {
    await supabase.from('admin_audit_log').insert({ action: 'scrape', actor: 'admin', ok: false, meta: { url, portal, reason: 'fetch_failed' }, at: new Date().toISOString() });
    return json({ ok: false, reason: 'fetch_failed' }, 502);
  }
  const id = btoa(url).slice(0, 24).replace(/=+$/, '');
  const row = { id, title: titleOf(html), price: priceOf(html), city: 'Berlin', url, portal, active: true, image: imageOf(html) || `https://picsum.photos/300/200?random=${id}`, synced_at: new Date().toISOString() };
  await supabase.from('listings').upsert(row, { onConflict: 'id' });
  await supabase.from('admin_audit_log').insert({ action: 'scrape', actor: 'admin', ok: true, meta: { url, portal, id }, at: new Date().toISOString() });
  return json({ ok: true, listing: row });
});
