-- supabase/migrations/001_schema.sql
-- 2HEART FINAL ADMIN SECURE 2FA — core schema
-- Run in Supabase SQL editor (or `supabase db push`).

create extension if not exists "pgcrypto";

-- WG listings aggregated from the 5 portals
create table if not exists public.listings (
  id          text primary key,
  title       text not null default '',
  price       integer not null default 0,
  city        text not null default '',
  url         text not null default '',
  image       text not null default '',
  portal      text not null default 'manual',
  active      boolean not null default true,
  synced_at   timestamptz not null default now()
);
create index if not exists listings_portal_idx on public.listings (portal);
create index if not exists listings_price_idx  on public.listings (price);
create index if not exists listings_active_idx on public.listings (active);

-- Immutable admin audit log (append-only; no UPDATE/DELETE policies in 002)
create table if not exists public.admin_audit_log (
  id      bigint generated always as identity primary key,
  action  text not null,
  actor   text not null default '',
  ok      boolean not null default false,
  meta    jsonb not null default '{}'::jsonb,
  at      timestamptz not null default now()
);
create index if not exists audit_at_idx on public.admin_audit_log (at desc);
create index if not exists audit_actor_idx on public.admin_audit_log (actor);
