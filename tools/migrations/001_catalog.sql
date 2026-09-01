-- fabPLAY catalog schema (additive only).
--
-- Source tables are READ-ONLY and never modified:
--   songs                        (id, song_name, storage_path, label)
--   songs_metadata_table_sample  (id, title, library, bpm, energy, ...)
--
-- Everything below is NEW. Nothing here alters existing tables.

-- ---------------------------------------------------------------------------
-- 1. song_mood_labels
--    pipeline_output is a huge TOASTed jsonb column (table is ~14 GB), so
--    reading mood_top_labels at query time is far too slow. Materialise the
--    labels once into a tiny table (id + text[]).
-- ---------------------------------------------------------------------------
create table if not exists public.song_mood_labels (
    id          bigint primary key,
    mood_labels text[] not null default '{}'
);

-- ---------------------------------------------------------------------------
-- 2. catalog_songs
--    The single shape the application consumes. Maps the two source tables
--    onto the field names the code expects.
--
--    Normalisation notes (verified against live data):
--      * energy   is ALREADY 0..1  -> passed through untouched.
--      * valence  is on a 1..9 scale -> normalised to 0..1 via (x-1)/8.
--      * arousal  is on a 1..9 scale -> normalised to 0..1 via (x-1)/8.
--    Raw values are also exposed as *_raw for debugging.
-- ---------------------------------------------------------------------------
create or replace view public.catalog_songs as
select
    s.id                                             as id,
    s.id                                             as song_id,
    coalesce(nullif(m.title, ''), s.song_name, '')   as title,
    s.label                                          as library,
    m.genre_label                                    as genre,
    m.song_type                                      as song_type,
    coalesce(s.storage_path, m.storage_object_key)   as url,
    coalesce(m.storage_bucket, 'songs')              as storage_bucket,
    m.duration_seconds                               as duration_seconds,
    m.bpm                                            as tempo_bpm,
    m.musical_key                                    as musical_key,

    least(greatest(m.energy, 0.0), 1.0)              as energy,
    least(greatest((m.valence - 1.0) / 8.0, 0.0), 1.0) as valence,
    least(greatest((m.arousal - 1.0) / 8.0, 0.0), 1.0) as arousal,
    m.valence                                        as valence_raw,
    m.arousal                                        as arousal_raw,

    m.danceability                                   as danceability,
    m.loudness                                       as loudness,
    m.acousticness                                   as acousticness,
    m.instrumentalness                               as instrumentalness,
    m.speechness                                     as speechness,
    m.onset_rate                                     as onset_rate,
    coalesce(ml.mood_labels, '{}')::text[]           as mood_predicted_labels
from public.songs s
join public.songs_metadata_table_sample m on m.id = s.id
left join public.song_mood_labels ml on ml.id = s.id
where m.status = 'completed'
  and coalesce(m.duration_seconds, 0) > 10
  and coalesce(m.bpm, 0) > 0;

-- ---------------------------------------------------------------------------
-- 3. analysis_song_features
--    Compatibility view for the embedding columns the retriever reads.
--    Vectors already exist for 100% of rows, so no backfill is required.
-- ---------------------------------------------------------------------------
create or replace view public.analysis_song_features as
select
    m.id              as song_id,
    m.maest_audio_768 as maest_audio_768,
    m.clap_audio_512  as clap_audio_512
from public.songs_metadata_table_sample m;

grant select on public.catalog_songs          to anon, authenticated, service_role;
grant select on public.analysis_song_features to anon, authenticated, service_role;
grant select on public.song_mood_labels       to anon, authenticated, service_role;
