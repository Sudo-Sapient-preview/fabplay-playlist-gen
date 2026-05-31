"""
Django-side Supabase client, startup checks, and catalog helpers.
"""

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Optional

from django.core.exceptions import ImproperlyConfigured
from dotenv import load_dotenv
from supabase import Client, create_client


BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")

logger = logging.getLogger(__name__)

_supabase: Optional[Client] = None
_supabase_lock = threading.Lock()

_HIDDEN_SONGS_FILE = Path(os.getenv("HIDDEN_SONGS_FILE", BASE_DIR / "data" / "hidden_songs.json"))
try:
    _HIDDEN_IDS: set[str] = set(json.loads(_HIDDEN_SONGS_FILE.read_text(encoding="utf-8")).get("hidden_ids", []))
except Exception:
    _HIDDEN_IDS = set()

_EXCLUDED_GENRES: set[str] = {"christian devotional"}

_SONG_COLS = (
    "id,title,artist,url,tempo_bpm,duration_seconds,"
    "energy,danceability,loudness,acousticness,instrumentalness,"
    "speechness,genre,valence,arousal,mood_predicted_labels,song_type"
)
_ENERGY_MAX = 0.15
_VALENCE_MAX = 9.0
_SONG_CACHE_TTL = 300.0

_song_cache: list[dict] = []
_song_cache_ts = 0.0


def _require_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise ImproperlyConfigured(f"{name} must be set.")
    return value


def _copy_rows(rows: list[dict]) -> list[dict]:
    return [dict(row) for row in rows]


def get_supabase() -> Client:
    global _supabase
    if _supabase is not None:
        return _supabase

    with _supabase_lock:
        if _supabase is None:
            _supabase = create_client(
                _require_env("SUPABASE_URL"),
                _require_env("SUPABASE_SERVICE_KEY"),
            )
    return _supabase


def check_pgvector_enabled() -> bool:
    try:
        result = get_supabase().rpc("check_pgvector_enabled").execute()
        return bool(result.data)
    except Exception:
        pass

    try:
        get_supabase().table("song_embeddings").select("id").limit(1).execute()
        return True
    except Exception:
        return False


def run_startup_checks(raise_on_fatal: bool = True) -> bool:
    try:
        songs_resp = get_supabase().table("songs").select("id", count="exact").execute()
        song_count = songs_resp.count or 0
    except Exception as exc:
        logger.error("Cannot query songs table during startup: %s", exc)
        if raise_on_fatal:
            raise RuntimeError("Cannot query songs table during startup.") from exc
        return False

    if song_count == 0:
        logger.error("Songs table is empty. Cannot generate playlists.")
        if raise_on_fatal:
            raise RuntimeError("Songs table is empty. Cannot generate playlists.")
        return False

    logger.info("Catalog ready: %s tracks available.", f"{song_count:,}")
    return True


def _normalize(songs: list[dict]) -> list[dict]:
    for song in songs:
        raw_energy = float(song.get("energy") or 0)
        raw_valence = float(song.get("valence") or 0)
        song["energy"] = min(raw_energy / _ENERGY_MAX, 1.0)
        song["valence"] = min(raw_valence / _VALENCE_MAX, 1.0)
    return songs


def _paginate(query) -> list[dict]:
    rows: list[dict] = []
    page_size = 1000
    offset = 0
    while True:
        batch = query.range(offset, offset + page_size - 1).execute().data or []
        rows.extend(batch)
        if len(batch) < page_size:
            break
        offset += page_size
    return rows


def _visible_songs(rows: list[dict]) -> list[dict]:
    return [
        row
        for row in rows
        if str(row.get("id")) not in _HIDDEN_IDS
        and (row.get("genre") or "").lower() not in _EXCLUDED_GENRES
    ]


def fetch_all_songs() -> list[dict]:
    global _song_cache, _song_cache_ts
    if _song_cache and (time.time() - _song_cache_ts) < _SONG_CACHE_TTL:
        return _copy_rows(_song_cache)

    rows = _normalize(_paginate(get_supabase().table("songs").select(_SONG_COLS)))
    rows = _visible_songs(rows)
    _song_cache = rows
    _song_cache_ts = time.time()
    return _copy_rows(rows)


def fetch_songs_filtered(
    tempo_min: float = 60,
    tempo_max: float = 180,
    energy_min: float = 0.0,
    energy_max: float = 1.0,
    valence_min: float = 0.0,
    valence_max: float = 1.0,
) -> list[dict]:
    query = (
        get_supabase()
        .table("songs")
        .select(_SONG_COLS)
        .gte("tempo_bpm", tempo_min)
        .lte("tempo_bpm", tempo_max)
        .gte("energy", energy_min)
        .lte("energy", energy_max)
        .gte("valence", valence_min)
        .lte("valence", valence_max)
    )
    rows = _normalize(_paginate(query))
    return _copy_rows(_visible_songs(rows))


def fetch_songs_not_yet_embedded() -> list[dict]:
    all_songs = fetch_all_songs()
    existing_resp = get_supabase().table("song_embeddings").select("song_id").execute()
    existing_ids = {row["song_id"] for row in (existing_resp.data or [])}
    return [song for song in all_songs if song["id"] not in existing_ids]


def upsert_embeddings(rows: list[dict]) -> None:
    get_supabase().table("song_embeddings").upsert(rows, on_conflict="song_id").execute()


def fetch_songs_without_maest() -> list[dict]:
    """Return songs that don't yet have a maest_audio_768 in analysis_song_features."""
    all_songs = fetch_all_songs()
    resp = (
        get_supabase()
        .table("analysis_song_features")
        .select("song_id")
        .not_.is_("maest_audio_768", "null")
        .execute()
    )
    done_ids = {row["song_id"] for row in (resp.data or [])}
    return [s for s in all_songs if str(s.get("id", "")) not in done_ids]


def upsert_maest_audio_768s(rows: list[dict]) -> None:
    """Write maest_audio_768 vectors into analysis_song_features.

    Updates existing rows and inserts rows for songs not yet in the table.
    Works without requiring a unique constraint on song_id.
    """
    client = get_supabase()
    song_ids = [r["song_id"] for r in rows]

    existing = (
        client.table("analysis_song_features")
        .select("song_id")
        .in_("song_id", song_ids)
        .execute()
    )
    existing_ids = {r["song_id"] for r in (existing.data or [])}

    for row in rows:
        if row["song_id"] in existing_ids:
            client.table("analysis_song_features").update(
                {"maest_audio_768": row["maest_audio_768"]}
            ).eq("song_id", row["song_id"]).execute()
        else:
            client.table("analysis_song_features").insert(row).execute()


def search_by_embedding(
    query_vector: list[float],
    match_count: int = 200,
    min_similarity: float = 0.30,
) -> list[dict]:
    vector_str = "[" + ",".join(str(value) for value in query_vector) + "]"

    try:
        result = get_supabase().rpc(
            "search_songs_by_embedding",
            {
                "query_embedding": vector_str,
                "match_count": match_count,
                "min_similarity": min_similarity,
            },
        ).execute()
    except Exception as exc:
        logger.error("search_songs_by_embedding RPC call failed: %s", exc)
        raise

    if hasattr(result, "error") and result.error:
        logger.error("search_songs_by_embedding RPC error: %s", result.error)
        raise RuntimeError(f"search_songs_by_embedding RPC failed: {result.error}")

    rows = result.data or []
    logger.info("search_songs_by_embedding returned %d rows (min_similarity=%.2f)", len(rows), min_similarity)
    return _copy_rows(rows)


def fetch_songs_by_artist(artist_name: str) -> list[dict]:
    result = (
        get_supabase()
        .table("songs")
        .select(_SONG_COLS)
        .ilike("artist", f"%{artist_name}%")
        .execute()
    )
    return _normalize(_copy_rows(result.data or []))


def fetch_songs_by_genre(genre_name: str) -> list[dict]:
    query_str = genre_name.lower()
    result = (
        get_supabase()
        .table("songs")
        .select(_SONG_COLS)
        .ilike("genre", f"%{query_str}%")
        .execute()
    )
    return _copy_rows(_visible_songs(_normalize(result.data or [])))
