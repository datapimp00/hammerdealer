# 2HEART FINAL — ADMIN SECURE 2FA

WG aggregator + secure admin. **Next.js 14** app (so `middleware.ts` actually runs) serving static single-file pages from `public/`, backed by Supabase Edge Functions + Postgres. Deploys to Vercel.

## Local development (runs the Edge middleware)

```bash
npm install
ADMIN_PASSWORD='dev-pw' ADMIN_EMAILS='you@x.com' npm run dev   # http://localhost:3000
```

`middleware.ts` protects `/admin/:path*`:
- No / wrong credentials → **401** + `WWW-Authenticate` challenge.
- Valid Basic Auth (email in `ADMIN_EMAILS` + correct `ADMIN_PASSWORD`) → issues the httpOnly `admin_session` cookie and serves the page.
- The 2FA page (`/admin/2fa`) is reachable without a session so factor 2 can be completed.

## Security — 3 Layers (MUST)

1. **Edge Basic Auth** (`middleware.ts`) — protects `/admin/:path*`.
   - Env `ADMIN_PASSWORD` + `ADMIN_EMAILS` whitelist.
   - HTTP Basic Auth over HTTPS → issues httpOnly, secure, SameSite=strict `admin_session` cookie (HMAC-signed, 1h).
   - Sends `X-Robots-Tag: noindex`, `X-Frame-Options: DENY`, `Cache-Control: no-store`.
2. **TOTP 2FA** (`supabase/functions/admin-auth`) — pure RFC 6238 HMAC-SHA1 via `crypto.subtle`, no libraries.
   - Actions: `setup-2fa`, `verify-2fa`, `verify`.
   - Writes an immutable audit log row on every attempt.
3. **Immutable Audit Log** — `admin_audit_log` has **no UPDATE/DELETE** policies (migration `002` adds `do instead nothing` rules as defense-in-depth).

## Stack & Design

- Static HTML + Vercel Edge Middleware + Supabase Edge Functions + Supabase Postgres.
- Neutral Pro `#0A84FF`, bg `#F5F5F7`, dark sidebar `#0A0A0A`, 16px rounded, `env(safe-area-inset-*)` padding.
- Live images only: `https://i.pravatar.cc/150?img=X` + `https://picsum.photos/300/200?random=X`.
- Apple-Pro style (Linear / Vercel / Supabase Studio). No print, no `Ctrl+P`.

## Layout

```
public/
  index.html              public aggregator (6 portals, searchable/sortable/paginated tables)
  admin/index.html        admin SPA
  admin/2fa.html          2FA page (setup + verify)
  manifest.json, icon*, robots.txt, sitemap.xml, impressum/datenschutz.html
middleware.ts             Layer 1 edge basic-auth (matcher /admin/:path*)
app/layout.tsx            minimal root layout (real UI lives in public/*.html)
app/health/route.ts       /health status JSON endpoint
next.config.mjs           security headers + clean-URL rewrites (/admin, /admin/2fa, SPA fallback)
package.json, tsconfig.json   Next.js 14 build/dev config
ENV.txt                   env var reference (no real secrets)
supabase/functions/       admin-auth, check-admin, 5x *-sync, scrape-listing
supabase/migrations/      001 schema, 002 RLS+immutable audit, 003 seed
supabase/cron-all.sql     pg_cron schedule for the 5 sync functions
```

> `vercel.json` was intentionally removed — rewrites & security headers now live in `next.config.mjs`, which Vercel reads for a Next.js project.

## Deploy

### 1. Database
```bash
supabase db push            # applies 001, 002, 003
# then run supabase/cron-all.sql once (after enabling pg_cron + pg_net)
```

### 2. Edge Functions
```bash
supabase functions deploy admin-auth
supabase functions deploy check-admin
supabase functions deploy wg-gesucht-sync
supabase functions deploy kleinanzeigen-sync
supabase functions deploy immoscout-sync
supabase functions deploy facebook-sync
supabase functions deploy scrape-listing
# all with --no-verify-jwt (auth handled by middleware + TOTP)
supabase secrets set ADMIN_PASSWORD=... ADMIN_EMAILS=... ADMIN_2FA_SECRET=...
supabase secrets set SUPABASE_URL=... SUPABASE_SERVICE_ROLE_KEY=... PROXY_LIST=...
```

### 3. Frontend
```bash
vercel --prod
# set Vercel env: ADMIN_PASSWORD, ADMIN_EMAILS
```

## First login
1. Open `/admin` → browser prompts Basic Auth (email + `ADMIN_PASSWORD`).
2. Go to `/admin/2fa` → `setup-2fa` to get the QR/secret, add to your authenticator.
3. `verify` with a 6-digit code → session granted for 1h.

## Notes / Assumptions
- The referenced `/mnt/data` build artifacts were not present, so the frontend HTML pages here are **minimal, fully-functional** single-file SPA implementations of the specified layout, design, and behaviour rather than copies of the large curated artifacts.
- TOTP seed is server-wide in this reference implementation; per-admin secrets should be stored hashed in a table for production.
