"""
Django-side Supabase client, startup checks, and catalog helpers.

Data model
----------
Two read-only source tables live in Supabase and are never written to:

    songs                        (id, song_name, storage_path, label)
    songs_metadata_table_sample  (id, title, library, bpm, energy, ...)

The application reads them through the ``catalog_songs`` view, which joins the
pair 1:1 on ``id`` and exposes the field names this codebase expects
(``title``, ``tempo_bpm``, ``genre``, ``url``, ``library`` ...).

Normalisation is done in SQL (see tools/migrations/001_catalog.sql):
    energy  -> already 0..1 in source, passed through
    valence -> source is 1..9, normalised to 0..1
    arousal -> source is 1..9, normalised to 0..1

There is no ``artist`` field anywhere in the catalog; ``library`` (Amurco /
Epic / Fabplay Originals) is the equivalent grouping dimension.
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

# Genres that must never appear in playlists / catalog UI.
_EXCLUDED_GENRES: set[str] = {
    "christian_devotional",
    "bollywood",
    "dawn",
    "goodbye",
    "nothing",
}

CATALOG_VIEW = "catalog_songs"

# Every library present in the catalog. All are enabled by default; the user
# may narrow the selection per brand.
ALL_LIBRARIES: tuple[str, ...] = ("Amurco", "Epic", "Fabplay Originals")

_SONG_COLS = (
    "id,song_id,title,library,genre,song_type,url,duration_seconds,"
    "tempo_bpm,musical_key,energy,valence,arousal,"
    "danceability,loudness,acousticness,instrumentalness,speechness,"
    "mood_predicted_labels"
)

_SONG_CACHE_TTL = float(os.getenv("SONG_CACHE_TTL", "300"))

_song_cache: list[dict] = []
_song_cache_ts = 0.0
_clap_cache: dict[str, list] = {}
_clap_cache_ts = 0.0
_cache_lock = threading.Lock()


def _require_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise ImproperlyConfigured(f"{name} must be set.")
    return value


def _copy_rows(rows: list[dict]) -> list[dict]:
    return [dict(row) for row in rows]


def as_count(value) -> int:
    """Coerce a PostgREST count into a plain int (0 when unavailable)."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def count_catalog(library: str | None = None) -> int:
    """Exact row count for the catalog, optionally scoped to one library."""
    query = get_supabase().table(CATALOG_VIEW).select("id", count="exact")
    if library:
        query = query.eq("library", library)
    return as_count(query.limit(1).execute().count)


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


def songs_base_url() -> str:
    """Public base URL for audio objects in the ``songs`` storage bucket."""
    configured = os.getenv("SONGS_BASE_URL", "").strip().rstrip("/")
    if configured:
        return configured
    base = os.getenv("SUPABASE_URL", "").strip().rstrip("/")
    bucket = os.getenv("SONGS_BUCKET", "songs").strip()
    return f"{base}/storage/v1/object/public/{bucket}" if base else ""


def run_startup_checks(raise_on_fatal: bool = True) -> bool:
    try:
        song_count = count_catalog()
    except Exception as exc:
        logger.error("Cannot query %s during startup: %s", CATALOG_VIEW, exc)
        if raise_on_fatal:
            raise RuntimeError(f"Cannot query {CATALOG_VIEW} during startup.") from exc
        return False

    if song_count == 0:
        logger.error("Catalog is empty. Cannot generate playlists.")
        if raise_on_fatal:
            raise RuntimeError("Catalog is empty. Cannot generate playlists.")
        return False

    logger.info("Catalog ready: %s tracks available.", f"{song_count:,}")
    return True


def _finalise(rows: list[dict]) -> list[dict]:
    """Attach derived fields and drop hidden/excluded rows."""
    out: list[dict] = []
    for row in rows:
        if str(row.get("id")) in _HIDDEN_IDS:
            continue
        if (row.get("genre") or "").lower() in _EXCLUDED_GENRES:
            continue
        row.setdefault("song_id", row.get("id"))
        if row.get("mood_predicted_labels") is None:
            row["mood_predicted_labels"] = []
        out.append(row)
    return out


def _paginate(query_factory, order_by: str = "id") -> list[dict]:
    """Page through a PostgREST result set.

    ``query_factory`` must return a *fresh* query object each call, because
    range() mutates the builder in place.

    An explicit ``order_by`` is essential. PostgREST caps every response at
    1000 rows, so the catalog must be read in pages; without a deterministic
    sort Postgres may return rows in a different order per request, which
    silently duplicates some rows across pages and drops others entirely.
    Sorting on the primary key makes the page boundaries stable.
    """
    rows: list[dict] = []
    seen: set = set()
    page_size = 1000
    offset = 0
    while True:
        batch = (
            query_factory()
            .order(order_by)
            .range(offset, offset + page_size - 1)
            .execute()
            .data
            or []
        )
        for row in batch:
            key = row.get(order_by)
            # Defensive: never let a repeated key through, even if the backend
            # hands us an overlapping page.
            if key is None:
                rows.append(row)
            elif key not in seen:
                seen.add(key)
                rows.append(row)
        if len(batch) < page_size:
            break
        offset += page_size
    return rows


def fetch_all_songs(libraries: list[str] | None = None) -> list[dict]:
    """Return the full playable catalog, optionally limited to given libraries."""
    global _song_cache, _song_cache_ts

    with _cache_lock:
        fresh = _song_cache and (time.time() - _song_cache_ts) < _SONG_CACHE_TTL
        if not fresh:
            rows = _finalise(
                _paginate(lambda: get_supabase().table(CATALOG_VIEW).select(_SONG_COLS))
            )
            _song_cache = rows
            _song_cache_ts = time.time()
        cached = _song_cache

    rows = _copy_rows(cached)
    return filter_by_library(rows, libraries)


def filter_by_library(rows: list[dict], libraries: list[str] | None) -> list[dict]:
    """Keep only rows whose library is selected. None/empty means all libraries."""
    if not libraries:
        return rows
    wanted = {str(lib).strip().lower() for lib in libraries if str(lib).strip()}
    if not wanted or wanted >= {lib.lower() for lib in ALL_LIBRARIES}:
        return rows
    return [r for r in rows if (r.get("library") or "").lower() in wanted]


def clear_song_cache() -> None:
    global _song_cache, _song_cache_ts, _clap_cache, _clap_cache_ts
    with _cache_lock:
        _song_cache = []
        _song_cache_ts = 0.0
        _clap_cache = {}
        _clap_cache_ts = 0.0


def fetch_all_clap_features() -> dict[str, list]:
    """Return clap_audio_512 for the whole catalog, cached in-process.

    Playlist generation scores the full catalog, so loading embeddings once
    avoids thousands of per-id PostgREST round-trips every day-part.
    """
    global _clap_cache, _clap_cache_ts

    with _cache_lock:
        fresh = _clap_cache and (time.time() - _clap_cache_ts) < _SONG_CACHE_TTL
        if fresh:
            return dict(_clap_cache)

    rows = _paginate(
        lambda: get_supabase()
        .table("analysis_song_features")
        .select("song_id,clap_audio_512"),
        order_by="song_id",
    )
    mapping: dict[str, list] = {}
    for row in rows:
        emb = row.get("clap_audio_512")
        if emb:
            mapping[str(row["song_id"])] = emb

    with _cache_lock:
        _clap_cache = mapping
        _clap_cache_ts = time.time()
    logger.info("Cached CLAP embeddings for %s tracks.", f"{len(mapping):,}")
    return dict(mapping)


def fetch_libraries() -> list[str]:
    """Library names present in the catalog, with track counts.

    PostgREST caps plain selects at 1000 rows, so a per-library exact count is
    used instead of scanning and de-duplicating the whole catalog.
    """
    counts = library_counts()
    found = [lib for lib in ALL_LIBRARIES if counts.get(lib, 0) > 0]
    return found or list(ALL_LIBRARIES)


def library_counts() -> dict[str, int]:
    """Track count per library. Always returns plain ints."""
    counts: dict[str, int] = {}
    try:
        for lib in ALL_LIBRARIES:
            counts[lib] = count_catalog(lib)
    except Exception as exc:
        logger.warning("library_counts failed: %s", exc)
    return counts


def fetch_songs_filtered(
    tempo_min: float = 60,
    tempo_max: float = 180,
    energy_min: float = 0.0,
    energy_max: float = 1.0,
    valence_min: float = 0.0,
    valence_max: float = 1.0,
    libraries: list[str] | None = None,
) -> list[dict]:
    def build():
        query = (
            get_supabase()
            .table(CATALOG_VIEW)
            .select(_SONG_COLS)
            .gte("tempo_bpm", tempo_min)
            .lte("tempo_bpm", tempo_max)
            .gte("energy", energy_min)
            .lte("energy", energy_max)
            .gte("valence", valence_min)
            .lte("valence", valence_max)
        )
        if libraries:
            query = query.in_("library", list(libraries))
        return query

    return _finalise(_paginate(build))


def fetch_songs_by_genre(genre_name: str, libraries: list[str] | None = None) -> list[dict]:
    """Genres are stored as coarse labels (electronic, rock, hip_hop, ...)."""
    needle = (genre_name or "").strip().lower().replace(" ", "_").replace("-", "_").replace("/", "_")
    if not needle:
        return []

    def build():
        query = get_supabase().table(CATALOG_VIEW).select(_SONG_COLS).ilike("genre", f"%{needle}%")
        if libraries:
            query = query.in_("library", list(libraries))
        return query

    return _finalise(_paginate(build))


def fetch_songs_by_library(library_name: str) -> list[dict]:
    def build():
        return get_supabase().table(CATALOG_VIEW).select(_SONG_COLS).eq("library", library_name)

    return _finalise(_paginate(build))


def fetch_analysis_features(song_ids: list[str], include_maest: bool = False) -> dict[str, dict]:
    """Fetch CLAP / MAEST vectors for the given song ids."""
    if not song_ids:
        return {}
    cols = "song_id,maest_audio_768,clap_audio_512" if include_maest else "song_id,clap_audio_512"
    client = get_supabase()
    features: dict[str, dict] = {}
    batch_size = 200
    for i in range(0, len(song_ids), batch_size):
        batch = [int(s) for s in song_ids[i : i + batch_size] if str(s).isdigit()]
        if not batch:
            continue
        rows = (
            client.table("analysis_song_features")
            .select(cols)
            .in_("song_id", batch)
            .execute()
            .data
            or []
        )
        for row in rows:
            features[str(row["song_id"])] = {
                "maest_audio_768": row.get("maest_audio_768"),
                "clap_audio_512": row.get("clap_audio_512"),
            }
    return features
