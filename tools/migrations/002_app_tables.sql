-- Application tables required by the Django app.
-- These are separate from the read-only song catalog and hold user/brand state.
--
-- Constraints are driven by what the code actually does:
--   brand_playlists     : upsert(on_conflict="brand_id") AND upsert(on_conflict="playlist_id")
--                         -> both columns need a UNIQUE constraint.
--   user_playlist_songs : delete().eq(user_id).eq(brand_id) then insert() -> plain insert, no upsert.
--   user_roles          : upsert(on_conflict="user_id") -> user_id is the primary key.

-- ---------------------------------------------------------------------------
-- brand_playlists : one saved playlist snapshot per brand (plus per playlist_id)
-- ---------------------------------------------------------------------------
create table if not exists public.brand_playlists (
    id            bigserial primary key,
    brand_id      text not null,
    playlist_id   text,
    user_id       uuid,
    playlist_name text,
    playlist_json jsonb not null default '{}'::jsonb,
    created_at    timestamptz not null default now(),
    updated_at    timestamptz not null default now()
);

-- upsert(on_conflict="brand_id") requires this to be unique.
create unique index if not exists brand_playlists_brand_id_key
    on public.brand_playlists (brand_id);

-- upsert(on_conflict="playlist_id") requires a FULL unique index: a partial
-- index (WHERE playlist_id is not null) cannot satisfy ON CONFLICT and fails
-- with 42P10. NULLs are distinct in Postgres, so multiple NULL rows are still
-- permitted by this index.
create unique index if not exists brand_playlists_playlist_id_key
    on public.brand_playlists (playlist_id);

create index if not exists brand_playlists_user_id_idx  on public.brand_playlists (user_id);
create index if not exists brand_playlists_updated_idx  on public.brand_playlists (updated_at desc);

-- ---------------------------------------------------------------------------
-- user_playlist_songs : flat per-user/per-brand list of songs used in a playlist
-- ---------------------------------------------------------------------------
create table if not exists public.user_playlist_songs (
    id           bigserial primary key,
    user_id      uuid not null,
    brand_id     text not null,
    brand_name   text,
    song_id      text not null,
    song_name    text,
    generated_at timestamptz not null default now()
);

create index if not exists user_playlist_songs_user_brand_idx
    on public.user_playlist_songs (user_id, brand_id);
create index if not exists user_playlist_songs_song_idx
    on public.user_playlist_songs (song_id);

-- ---------------------------------------------------------------------------
-- user_roles : superadmin / admin / viewer
-- ---------------------------------------------------------------------------
create table if not exists public.user_roles (
    user_id    uuid primary key,
    role       text not null default 'viewer'
               check (role in ('superadmin', 'admin', 'viewer')),
    updated_at timestamptz not null default now()
);

-- ---------------------------------------------------------------------------
-- Keep updated_at fresh on write.
-- ---------------------------------------------------------------------------
create or replace function public.set_updated_at()
returns trigger
language plpgsql
as $$
begin
    new.updated_at = now();
    return new;
end;
$$;

drop trigger if exists brand_playlists_set_updated_at on public.brand_playlists;
create trigger brand_playlists_set_updated_at
    before update on public.brand_playlists
    for each row execute function public.set_updated_at();

drop trigger if exists user_roles_set_updated_at on public.user_roles;
create trigger user_roles_set_updated_at
    before update on public.user_roles
    for each row execute function public.set_updated_at();

-- ---------------------------------------------------------------------------
-- RLS: the app connects with the service role key, which bypasses RLS.
-- Enabling RLS with no permissive policy blocks anon/authenticated clients
-- from reading other users' data directly via PostgREST.
-- ---------------------------------------------------------------------------
alter table public.brand_playlists     enable row level security;
alter table public.user_playlist_songs enable row level security;
alter table public.user_roles          enable row level security;

grant select on public.catalog_songs          to anon, authenticated;
grant select on public.analysis_song_features to anon, authenticated;
