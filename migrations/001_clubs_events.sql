-- Phase 1: clubs and events. Re-runnable (IF NOT EXISTS), so `manage.py db-init`
-- is safe to point at staging first and then production.
--
-- gen_random_uuid() needs pgcrypto on older Postgres; Supabase has it enabled.
create extension if not exists pgcrypto;

create table if not exists clubs (
  id            uuid primary key default gen_random_uuid(),
  slug          text unique not null,               -- [a-z0-9-], the url segment
  name          text not null,
  password_hash text not null,                       -- werkzeug hash, never plaintext
  status        text not null default 'pending'      -- pending | approved | suspended
                check (status in ('pending', 'approved', 'suspended')),
  quota_bytes   bigint not null default 104857600,   -- 100 MB
  created_at    timestamptz not null default now()
);

create table if not exists events (
  id               uuid primary key default gen_random_uuid(),
  club_id          uuid not null references clubs(id) on delete cascade,
  slug             text not null,
  name             text not null,
  config           jsonb not null default '{}'::jsonb,   -- the frozen Phase 0.5 fields shape
  active           boolean not null default false,
  template_ext     text,
  template_version text,
  created_at       timestamptz not null default now(),
  unique (club_id, slug)                                 -- slugs unique WITHIN a club
);

-- tenant id first, so the index serves "this club's events, newest first"
-- (leftmost-prefix; a (created_at, club_id) index would not).
create index if not exists events_club_created_idx on events (club_id, created_at desc);
