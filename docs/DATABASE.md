# Database & Storage

## Source tables (read-only — never modified)

Both live in Supabase project `iruimptvnjrpmgtebjzq` and hold **41,519 rows each**,
joined 1:1 on `id` (verified: 0 orphans, 0 label mismatches, 0 path mismatches).

| Table | Columns |
|---|---|
| `songs` | `id`, `song_name`, `storage_path`, `label` |
| `songs_metadata_table_sample` | `id`, `title`, `library`, `bpm`, `energy`, `valence`, `arousal`, `genre_label`, `song_type`, `duration_seconds`, `danceability`, `loudness`, `acousticness`, `instrumentalness`, `speechness`, `onset_rate`, `musical_key`, `maest_audio_768`, `clap_audio_512`, `maest_embedding`, `pipeline_output`, `status`, … |

`songs.label` and `songs_metadata_table_sample.library` always agree.

## Libraries

| Library | Tracks |
|---|---|
| Epic | 20,828 |
| Amurco | 18,663 |
| Fabplay Originals | 2,005 |

> There is **no `FPO`** value in the data — the third library is stored as the
> literal string **`Fabplay Originals`**. All three are enabled by default and
> the user can narrow the selection per brand.

## Objects created by this project (additive only)

Applied via `tools/migrations/001_catalog.sql`.

### `song_mood_labels` (table)
`pipeline_output` is a TOASTed jsonb column on a ~14 GB table, so extracting
`mood_top_labels` at query time times out. The labels are materialised once into
`(id bigint pk, mood_labels text[])`. Backfilled for all 41,519 rows via
`tools/backfill_moods.py` (idempotent, safe to re-run).

### `catalog_songs` (view)
The single shape the application reads. Joins the two source tables and renames
columns to what the code expects: `title`, `library`, `genre` (←`genre_label`),
`tempo_bpm` (←`bpm`), `url` (←`storage_path`), `mood_predicted_labels`.

Excludes rows with `status <> 'completed'`, `duration_seconds <= 10`, `bpm <= 0`,
or pure-silence assets → **41,474 playable tracks** (45 excluded).

**Silence removal (destructive, completed):** 24 tracks were pure digital
silence and have been **permanently deleted** from Storage, `songs`,
`songs_metadata_table_sample` and `song_mood_labels`.

Each was verified by decoding the audio with ffmpeg (`volumedetect`) and
requiring peak `max_volume <= -60 dBFS` — all 24 measured -80.8 dB or lower.
A byte-level inspection is **not** a valid test here: compressed MP3 frames
encoding silence still contain mostly non-zero bytes.

- 22 × Fabplay Originals utility files (`1 Hour Silence 1..6`, `1 Min Silence 1..16`)
- `7087` River To The Sea (Amurco) — 0.6 s, truncated upload
- `8049` Just Ask Me (Amurco) — 4.5 min file that is silent end-to-end

Row counts went 41,519 -> **41,495** across all three tables (still 1:1).
Backups: `backups/silence_backup_*.json` (metadata) and
`backups/silence_audio/` (the 24 MP3s, 75.6 MB).

Genuinely quiet music is unaffected — e.g. `Ice Cracks` (-45 dB) and real songs
with "Silence" in the title (`A Northern Sort of Silence`) are **kept**.

The `loudness = 0 and energy < 0.001` guard in `003_exclude_silence.sql` is
retained as a safety net so any future silent ingest is auto-excluded; it
currently matches 0 rows.

**Still excluded by the view (21 rows):** short "sting"/FX cues of 1–10 s
(e.g. `Sting Fairy`, `Jazzy Piano Stinger 1`). These are intentional — they are
real audio but too short to program into a playlist.

**Normalisation (important):**

| Field | Source range | Exposed |
|---|---|---|
| `energy` | already `0–1` | passed through unchanged |
| `valence` | `1–9` | `(x-1)/8` → `0–1` (raw kept as `valence_raw`) |
| `arousal` | `1–9` | `(x-1)/8` → `0–1` (raw kept as `arousal_raw`) |

> The previous code divided `energy` by `0.15` and clamped to `1.0`, which
> collapsed 55% of the catalog to exactly `1.0` and destroyed energy ranking.
> That normalisation has been removed.

### `analysis_song_features` (view)
Exposes `song_id`, `maest_audio_768` (768-dim), `clap_audio_512` (512-dim).
Both vectors are populated for **100%** of rows, so no embedding backfill is needed.

## Application tables (created by `002_app_tables.sql`)

These hold user/brand state and are separate from the song catalog.

| Table | Purpose | Key constraint |
|---|---|---|
| `brand_playlists` | saved playlist snapshots | UNIQUE on **both** `brand_id` and `playlist_id` — the code upserts with `on_conflict` on each |
| `user_playlist_songs` | per-user/brand song usage | indexed on `(user_id, brand_id)` |
| `user_roles` | `superadmin` / `admin` / `viewer` | `user_id` primary key |

> The `playlist_id` unique index must be a **full** index, not partial.
> A partial index (`WHERE playlist_id is not null`) cannot satisfy
> `ON CONFLICT (playlist_id)` and fails with Postgres error `42P10`.

RLS is enabled on all three. The app uses the service-role key, which bypasses
RLS; anon/authenticated clients cannot read them directly.

## Not used

- `song_embeddings` table and the `search_songs_by_embedding` / `check_pgvector_enabled`
  RPCs do not exist and are not required. Retrieval is feature/MMR based
  (`pipeline/rag_retriever.py` never called vector search).
- **There is no `artist` column anywhere** — it is `null` throughout
  `pipeline_output`. `library` is the grouping dimension used instead.

## Storage

Bucket `songs` is **public**. Objects are named `{id}.mp3` matching
`songs.storage_path`. Playback URLs are built as:

```
{SUPABASE_URL}/storage/v1/object/public/songs/{id}.mp3
```

`SONGS_BASE_URL` is derived automatically from `SUPABASE_URL` when not set
explicitly (see `fabplay/settings.py`).

## LLM

Azure OpenAI has been fully removed. All calls go through **OpenRouter**
(OpenAI-compatible client) using `OPENROUTER_API_KEY` / `OPENROUTER_MODEL`.

## Running migrations

```bash
venv/Scripts/python.exe tools/sql.py tools/migrations/001_catalog.sql
venv/Scripts/python.exe tools/backfill_moods.py          # one-time, ~200s
venv/Scripts/python.exe tools/sql.py tools/migrations/002_app_tables.sql
venv/Scripts/python.exe tools/sql.py tools/migrations/003_exclude_silence.sql
```

## Removing silent tracks

```bash
venv/Scripts/python.exe tools/verify_silence.py   # decodes every candidate, writes silence_ids.txt
venv/Scripts/python.exe tools/delete_silence.py            # dry run
venv/Scripts/python.exe tools/delete_silence.py --apply    # delete storage + rows
```

`verify_silence.py` refuses to emit an ID list if any candidate contains audible
sound or fails to decode. `delete_silence.py` re-checks `loudness <> 0` before
acting, and removes storage objects *before* DB rows so the two cannot drift.

`tools/sql.py` uses the Supabase Management API with `SUPABASE_TOKEN`.
