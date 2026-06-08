"""
db.py — Supabase client, startup checks, and data helpers for fabPLAY v2.0
"""

import os
import sys
import json
from pathlib import Path
from supabase import create_client, Client
from typing import Optional
from dotenv import load_dotenv

load_dotenv()

_supabase: Optional[Client] = None

# Load hidden song IDs
_HIDDEN_SONGS_FILE = Path(__file__).parent / "hidden_songs.json"
try:
    _HIDDEN_IDS: set[str] = set(json.loads(_HIDDEN_SONGS_FILE.read_text()).get("hidden_ids", []))
except Exception:
    _HIDDEN_IDS: set[str] = set()

# Genres excluded from all playlists regardless of brand settings
_EXCLUDED_GENRES: set[str] = {"christian devotional"}


def get_supabase() -> Client:
    """Return (and lazily create) the shared Supabase client."""
    global _supabase
    if _supabase is None:
        url = os.getenv("SUPABASE_URL")
        key = os.getenv("SUPABASE_SERVICE_KEY")
        if not url or not key:
            print("[ERROR] SUPABASE_URL and SUPABASE_SERVICE_KEY must be set in .env")
            sys.exit(1)
        _supabase = create_client(url, key)
    return _supabase


def reset_supabase() -> Client:
    """Force-recreate the Supabase client (e.g. after a connection drop) and return new instance."""
    global _supabase
    _supabase = None
    return get_supabase()


# ─── Startup checks ──────────────────────────────────────────────────────────

def check_pgvector_enabled() -> bool:
    """
    Return True if the pgvector extension is enabled.
    Checks via the custom RPC first; falls back to querying pg_extension directly
    so this works even before setup_vectors.sql has been fully run.
    """
    # Try custom RPC (available after setup_vectors.sql is run)
    try:
        result = get_supabase().rpc("check_pgvector_enabled").execute()
        return bool(result.data)
    except Exception:
        pass

    # Fallback: check if song_embeddings table exists (implies pgvector + setup done)
    try:
        get_supabase().table("song_embeddings").select("id").limit(1).execute()
        return True  # table exists → pgvector was enabled and setup ran
    except Exception:
        pass

    return False


def run_startup_checks(exit_on_fatal: bool = True) -> None:
    """
    Verify the system is ready before accepting user input.
    - songs table is populated
    """
    supabase = get_supabase()

    try:
        songs_resp = supabase.table("analysis_song_features").select("song_id", count="exact").execute()
        song_count = songs_resp.count or 0
    except Exception as e:
        print(f"[ERROR] Cannot query analysis_song_features table: {e}")
        if exit_on_fatal:
            sys.exit(1)
        return

    if song_count == 0:
        print("[ERROR] analysis_song_features table is empty. Cannot generate playlists.")
        if exit_on_fatal:
            sys.exit(1)
        return

    print(f"[OK] Catalog ready: {song_count:,} tracks available.")


# ─── Data fetch helpers ───────────────────────────────────────────────────────

_SONG_COLS = (
    "song_id,title,artist,url,bpm,duration_seconds,"
    "energy,danceability,loudness,acousticness,instrumentalness,"
    "speechness,genre,valence,song_type"
)

# DB stores energy on ~0–0.15 scale and valence on ~0–9 scale.
# Normalize both to 0–1 so the pipeline's filters and MMR work correctly.
_ENERGY_MAX  = 0.15
_VALENCE_MAX = 9.0


def _remap_songs(songs: list[dict]) -> list[dict]:
    """Remap analysis_song_features column names to the field names the pipeline expects."""
    remapped = []
    for s in songs:
        remapped.append({
            "id":               s.get("song_id"),
            "song_id":          s.get("song_id"),
            "title":            s.get("title"),
            "artist":           (s.get("artist") or "").lower(),
            "url":              s.get("url"),
            "tempo_bpm":        s.get("bpm"),
            "duration_seconds": s.get("duration_seconds"),
            "energy":           s.get("energy"),
            "danceability":     s.get("danceability"),
            "loudness":         s.get("loudness"),
            "acousticness":     s.get("acousticness"),
            "instrumentalness": s.get("instrumentalness"),
            "speechness":       s.get("speechness") or 0.0,
            "genre":            s.get("genre"),
            "valence":          s.get("valence"),
            "song_type":        s.get("song_type") or "",
        })
    return remapped


def _normalize(songs: list[dict]) -> list[dict]:
    for s in songs:
        raw_e = float(s.get("energy")  or 0)
        raw_v = float(s.get("valence") or 0)
        s["energy"]  = min(raw_e / _ENERGY_MAX,  1.0)
        s["valence"] = min(raw_v / _VALENCE_MAX, 1.0)
    return songs


def _paginate(query) -> list[dict]:
    """Run a Supabase query with pagination and return all rows."""
    all_rows = []
    page_size = 1000
    offset = 0
    while True:
        batch = (query.range(offset, offset + page_size - 1).execute().data or [])
        all_rows.extend(batch)
        if len(batch) < page_size:
            break
        offset += page_size
    return all_rows


_song_cache: list[dict] = []
_song_cache_ts: float = 0.0
_SONG_CACHE_TTL = 300.0   # 5 minutes


def fetch_all_songs() -> list[dict]:
    """Fetch all rows from the songs table, normalized and cached for 5 minutes."""
    import time
    global _song_cache, _song_cache_ts
    if _song_cache and (time.time() - _song_cache_ts) < _SONG_CACHE_TTL:
        return list(_song_cache)
    for attempt in range(2):
        try:
            rows = _normalize(_remap_songs(_paginate(get_supabase().table("analysis_song_features").select(_SONG_COLS))))
            break
        except Exception as e:
            if attempt == 0 and any(kw in str(e).lower() for kw in ("disconnect", "connection")):
                reset_supabase()
                continue
            raise
    rows = [
        s for s in rows
        if str(s.get("id")) not in _HIDDEN_IDS
        and (s.get("genre") or "").lower() not in _EXCLUDED_GENRES
    ]
    _song_cache = rows
    _song_cache_ts = time.time()
    return list(rows)


_valid_song_ids: set[str] = set()
_valid_song_ids_ts: float = 0.0
_VALID_IDS_TTL = 300.0   # 5 minutes


def fetch_valid_song_ids() -> set[str]:
    """Return the set of song IDs that exist in the `songs` table (the FK target
    for user_playlist_songs.song_id), cached for 5 minutes.

    The catalog (analysis_song_features, ~19.5k) is a superset of `songs` (~18.5k);
    ~1k tracks live only in analysis_song_features. Inserting those into
    user_playlist_songs violates the FK, so callers pre-filter against this set.
    Returned IDs are normalised to str so membership tests are type-agnostic.
    """
    import time
    global _valid_song_ids, _valid_song_ids_ts
    if _valid_song_ids and (time.time() - _valid_song_ids_ts) < _VALID_IDS_TTL:
        return _valid_song_ids
    for attempt in range(2):
        try:
            rows = _paginate(get_supabase().table("songs").select("id"))
            break
        except Exception as e:
            if attempt == 0 and any(kw in str(e).lower() for kw in ("disconnect", "connection")):
                reset_supabase()
                continue
            raise
    _valid_song_ids = {str(r["id"]) for r in rows if r.get("id") is not None}
    _valid_song_ids_ts = time.time()
    return _valid_song_ids


def fetch_songs_filtered(
    tempo_min: float = 60,   tempo_max: float = 180,
    energy_min: float = 0.0, energy_max: float = 1.0,
    valence_min: float = 0.0, valence_max: float = 1.0,
) -> list[dict]:
    """Fetch only songs matching the numeric bounds — avoids pulling the full 18k catalog."""
    q = (
        get_supabase()
        .table("analysis_song_features")
        .select(_SONG_COLS)
        .gte("tempo_bpm",  tempo_min)
        .lte("tempo_bpm",  tempo_max)
        .gte("energy",     energy_min)
        .lte("energy",     energy_max)
        .gte("valence",    valence_min)
        .lte("valence",    valence_max)
    )
    rows = _paginate(q)
    return [
        s for s in rows
        if str(s.get("id")) not in _HIDDEN_IDS
        and (s.get("genre") or "").lower() not in _EXCLUDED_GENRES
    ]


def fetch_songs_not_yet_embedded() -> list[dict]:
    """Fetch only songs that do not yet have an entry in song_embeddings."""
    all_songs = fetch_all_songs()
    existing_resp = (
        get_supabase().table("song_embeddings").select("song_id").execute()
    )
    existing_ids = {row["song_id"] for row in (existing_resp.data or [])}
    return [s for s in all_songs if s["id"] not in existing_ids]


def upsert_embeddings(rows: list[dict]) -> None:
    """
    Upsert embedding rows into song_embeddings.
    rows: list of dicts with keys: song_id, embedding, embed_text, model
    on_conflict='song_id' makes re-runs safe (update existing, insert new).
    """
    get_supabase().table("song_embeddings").upsert(
        rows, on_conflict="song_id"
    ).execute()


# ─── Vector search RPC wrapper ────────────────────────────────────────────────

def search_by_embedding(
    query_vector: list[float],
    match_count:  int   = 200,
    min_similarity: float = 0.30,
) -> list[dict]:
    """
    Call the search_songs_by_embedding RPC function in Supabase.
    Returns a list of dicts: song fields + 'similarity' key.
    """
    import logging as _logging
    _log = _logging.getLogger(__name__)

    # pgvector requires the vector as a formatted string "[f1,f2,...]"
    vector_str = "[" + ",".join(str(x) for x in query_vector) + "]"

    try:
        result = get_supabase().rpc(
            "search_songs_by_embedding",
            {
                "query_embedding": vector_str,
                "match_count":     match_count,
                "min_similarity":  min_similarity,
            },
        ).execute()
    except Exception as e:
        _log.error("[search_by_embedding] RPC call raised exception: %s", e)
        raise

    if hasattr(result, "error") and result.error:
        _log.error("[search_by_embedding] RPC error: %s", result.error)
        raise RuntimeError(f"search_songs_by_embedding RPC failed: {result.error}")

    rows = result.data or []
    _log.warning("[search_by_embedding] RPC returned %d rows (min_sim=%.2f)", len(rows), min_similarity)
    return rows


def _norm_genre(g: str) -> str:
    """Lowercase and unify separators so 'Hip-Hop', 'hip hop' and 'hip_hop' all match."""
    return (g or "").lower().replace(" ", "_").replace("/", "_").replace("-", "_")


def fetch_songs_by_artist(artist_name: str) -> list[dict]:
    """Substring artist match against the in-memory catalog cache (no DB round-trip).

    Artists in the cache are already lower-cased by _remap_songs, so this is a
    plain case-insensitive substring test. Used as the must-include fallback when
    an artist isn't already in the candidate pool.
    """
    needle = (artist_name or "").strip().lower()
    if not needle:
        return []
    return [s for s in fetch_all_songs() if needle in (s.get("artist") or "")]


def fetch_songs_by_genre(genre_name: str) -> list[dict]:
    """Normalised genre match against the in-memory catalog cache (no DB round-trip).

    Matching is separator-agnostic ('hip-hop' matches the catalog's 'hip_hop'),
    which both fixes hyphen/underscore mismatches and removes the per-genre DB
    ilike queries that previously ran once per day-part for genres absent from the
    candidate pool. _HIDDEN_IDS / _EXCLUDED_GENRES are already applied by fetch_all_songs.
    """
    target = _norm_genre(genre_name)
    if not target:
        return []
    return [s for s in fetch_all_songs() if target in _norm_genre(s.get("genre", ""))]
