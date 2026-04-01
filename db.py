"""
db.py — Supabase client, startup checks, and data helpers for fabPLAY v2.0
"""

import os
import sys
from supabase import create_client, Client
from dotenv import load_dotenv

load_dotenv()

_supabase: Client | None = None


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
        songs_resp = supabase.table("songs").select("id", count="exact").execute()
        song_count = songs_resp.count or 0
    except Exception as e:
        print(f"[ERROR] Cannot query songs table: {e}")
        if exit_on_fatal:
            sys.exit(1)
        return

    if song_count == 0:
        print("[ERROR] songs table is empty. Cannot generate playlists.")
        if exit_on_fatal:
            sys.exit(1)
        return

    print(f"[OK] Catalog ready: {song_count:,} tracks available.")


# ─── Data fetch helpers ───────────────────────────────────────────────────────

def fetch_all_songs() -> list[dict]:
    """Fetch all rows from the songs table (paginates past the 1000-row default limit)."""
    all_rows = []
    page_size = 1000
    offset = 0
    while True:
        result = get_supabase().table("songs").select("*").range(offset, offset + page_size - 1).execute()
        batch = result.data or []
        all_rows.extend(batch)
        if len(batch) < page_size:
            break
        offset += page_size
    return all_rows


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


def fetch_songs_by_artist(artist_name: str) -> list[dict]:
    """
    Direct SQL fallback for must-include artists not found in ANN results.
    Uses ILIKE for case-insensitive partial match.
    """
    result = (
        get_supabase()
        .table("songs")
        .select("*")
        .ilike("artist", f"%{artist_name}%")
        .execute()
    )
    return result.data or []


def fetch_songs_by_genre(genre_name: str) -> list[dict]:
    """
    Direct SQL fetch for must-include genres not found in ANN results.
    Uses ILIKE for case-insensitive partial match.
    """
    result = (
        get_supabase()
        .table("songs")
        .select("*")
        .ilike("genre", f"%{genre_name}%")
        .execute()
    )
    return result.data or []
