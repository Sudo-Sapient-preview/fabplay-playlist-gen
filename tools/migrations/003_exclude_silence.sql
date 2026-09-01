-- Exclude non-musical "dead air" assets from the playable catalog.
--
-- The Fabplay Originals library ships 22 utility files ("1 Hour Silence 1..6",
-- "1 Min Silence 1..16"). They are pure digital silence and must never be
-- selected into a playlist.
--
-- Detection: loudness = 0 exactly AND energy ~ 0. Verified this matches exactly
-- those 22 rows and nothing else. Genuinely quiet music (e.g. "Ice Cracks",
-- -45 dB) is NOT affected, and real songs with "Silence" in the title
-- (e.g. "A Northern Sort of Silence") are kept.

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
  and coalesce(m.bpm, 0) > 0
  -- drop pure-silence utility assets
  and not (coalesce(m.loudness, -1) = 0 and coalesce(m.energy, 1) < 0.001);

grant select on public.catalog_songs to anon, authenticated, service_role;
