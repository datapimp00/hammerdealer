-- supabase/migrations/002_rls_immutable_audit.sql
-- 2HEART FINAL ADMIN SECURE 2FA — Row Level Security + immutable audit log.
-- CRITICAL: admin_audit_log has ONLY insert/select. No UPDATE, no DELETE policies.

alter table public.listings        enable row level security;
alter table public.admin_audit_log  enable row level security;

-- ---------- listings ----------
-- Public read of active listings only.
drop policy if exists "listings_public_read" on public.listings;
create policy "listings_public_read"
  on public.listings for select
  using (active = true);

-- Service role (edge functions) may insert/update/delete.
drop policy if exists "listings_service_write" on public.listings;
create policy "listings_service_write"
  on public.listings for all
  using (auth.role() = 'service_role')
  with check (auth.role() = 'service_role');

-- ---------- admin_audit_log (immutable) ----------
-- Insert + select for service_role only. Intentionally NO update/delete policy.
drop policy if exists "audit_service_insert" on public.admin_audit_log;
create policy "audit_service_insert"
  on public.admin_audit_log for insert
  with check (auth.role() = 'service_role');

drop policy if exists "audit_service_select" on public.admin_audit_log;
create policy "audit_service_select"
  on public.admin_audit_log for select
  using (auth.role() = 'service_role');

-- Defense-in-depth: block UPDATE and DELETE via rules regardless of policies.
drop rule if exists audit_no_update on public.admin_audit_log;
create rule audit_no_update as on update to public.admin_audit_log do instead nothing;
drop rule if exists audit_no_delete on public.admin_audit_log;
create rule audit_no_delete as on delete to public.admin_audit_log do instead nothing;
