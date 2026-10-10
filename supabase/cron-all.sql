-- supabase/cron-all.sql
-- 2HEART FINAL ADMIN SECURE 2FA — schedule all 5 WG sync functions via pg_cron + pg_net.
-- Requires: Supabase project with extensions `pg_cron` and `pg_net` enabled.
-- Set these once (Dashboard -> SQL editor, run as postgres):
--   create extension if not exists pg_cron;
--   create extension if not exists pg_net;
-- Replace <PROJECT_REF> and the ANON key with your project values before running.

create or replace function public.heart_sync(portal text, fn text) returns void
language plpgsql security definer as $$
begin
  perform net.http_post(
    url     := format('https://fnrwqovopebrluhmmgcu.supabase.co/functions/v1/%s', fn),
    headers := jsonb_build_object('Content-Type', 'application/json', 'Authorization', 'Bearer ' || current_setting('app.settings.anon_key', true)),
    body    := jsonb_build_object('portal', portal)
  );
end;
$$;

-- Every 15 minutes: run the 4 portal syncs. Stagger by minute to avoid bursts.
select cron.schedule('heart-wg-gesucht',  '*/15 * * * *', $$select public.heart_sync('wg-gesucht','wg-gesucht-sync')$$);
select cron.schedule('heart-kleinanzeigen','2,17,32,47 * * * *', $$select public.heart_sync('kleinanzeigen','kleinanzeigen-sync')$$);
select cron.schedule('heart-immoscout',    '4,19,34,49 * * * *', $$select public.heart_sync('immoscout','immoscout-sync')$$);
select cron.schedule('heart-facebook',     '6,21,36,51 * * * *', $$select public.heart_sync('facebook','facebook-sync')$$);

-- List / unschedule helpers:
--   select * from cron.job;
--   select cron.unschedule('heart-wg-gesucht');
