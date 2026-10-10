// supabase/functions/immoscout-sync/index.ts
// Layer: WG aggregator sync for immoscout. Cron-driven (see supabase/cron-all.sql).
// Fetches offers, normalises, upserts into `listings`, writes immutable audit log.
// Deploy: supabase functions deploy immoscout-sync --no-verify-jwt
import { createClient } from 'https://esm.sh/@supabase/supabase-js@2';

const SUPABASE_URL = Deno.env.get('SUPABASE_URL')!;
const SERVICE_ROLE = Deno.env.get('SUPABASE_SERVICE_ROLE_KEY')!;
const PORTAL = 'immoscout';
const SOURCE = Deno.env.get('IMMOSCOUT_URL') ?? 'https://www.immobilienscout24.de/Suche/de/wohnung-mieten';
const PROXY = (Deno.env.get('PROXY_LIST') ?? '').split(',').filter(Boolean);

const cors = { 'Access-Control-Allow-Origin': '*', 'Access-Control-Allow-Headers': 'authorization, x-client-info, apikey, content-type' };
const json = (b: unknown, s = 200) => new Response(JSON.stringify(b), { status: s, headers: { ...cors, 'Content-Type': 'application/json' } });

function pickProxy(): string | null {
  if (!PROXY.length) return null;
  return PROXY[Math.floor(Math.random() * PROXY.length)];
}
async function fetchHtml(url: string): Promise<string | null> {
  try {
    const p = pickProxy();
    const headers: Record<string, string> = { 'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) 2HEARTBot/1.0' };
    if (p) headers['x-proxy-hint'] = p;
    const res = await fetch(url, { headers });
    return res.ok ? await res.text() : null;
  } catch { return null; }
}
function parse(html: string) {
  const items: { id: string; title: string; price: number; city: string; url: string; image: string }[] = [];
  const re = /<a[^>]+href="([^"]+)"[^>]*>([^<]{4,80})<\/a>/g;
  let m: RegExpExecArray | null; const seen = new Set<string>();
  while ((m = re.exec(html)) && items.length < 50) {
    const url = m[1]; const title = m[2].trim();
    if (!/wohn|zimmer|wg|haus|miete/i.test(title)) continue;
    if (seen.has(url)) continue; seen.add(url);
    const priceMatch = html.slice(m.index, m.index + 400).match(/(\d{2,4})\s*(?:€|EUR)/);
    const id = btoa(url).slice(0, 24).replace(/=+$/, '');
    items.push({ id, title, price: priceMatch ? parseInt(priceMatch[1], 10) : 0, city: 'Berlin', url: url.startsWith('http') ? url : `https://www.immobilienscout24.de${url}`, image: `https://picsum.photos/300/200?random=${id}` });
  }
  return items;
}
Deno.serve(async (req) => {
  if (req.method === 'OPTIONS') return new Response('ok', { headers: cors });
  const supabase = createClient(SUPABASE_URL, SERVICE_ROLE);
  const started = Date.now();
  const html = await fetchHtml(SOURCE);
  if (!html) {
    await supabase.from('admin_audit_log').insert({ action: 'sync', actor: 'cron', ok: false, meta: { portal: PORTAL, reason: 'fetch_failed' }, at: new Date().toISOString() });
    return json({ ok: false, portal: PORTAL, reason: 'fetch_failed' }, 502);
  }
  const items = parse(html);
  if (items.length) await supabase.from('listings').upsert(items.map((it) => ({ ...it, portal: PORTAL, active: true, synced_at: new Date().toISOString() })), { onConflict: 'id' });
  await supabase.from('admin_audit_log').insert({ action: 'sync', actor: 'cron', ok: true, meta: { portal: PORTAL, count: items.length, ms: Date.now() - started }, at: new Date().toISOString() });
  return json({ ok: true, portal: PORTAL, count: items.length });
});
