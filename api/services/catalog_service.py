from api.db import (
    ALL_LIBRARIES,
    CATALOG_VIEW,
    count_catalog,
    fetch_all_songs,
    get_supabase,
    library_counts,
)
from api.utils import resolve_track_src


def get_genres() -> dict:
    """Coarse genre labels present in the catalog (electronic, rock, ...)."""
    try:
        songs = fetch_all_songs()
        genres = sorted({song.get("genre") for song in songs if song.get("genre")})
        return {"genres": genres}
    except Exception as exc:
        return {"genres": [], "error": str(exc)}


def get_libraries() -> dict:
    """Available libraries and their track counts.

    The catalog has no artist dimension; library is the grouping the UI offers.
    All libraries are enabled by default.
    """
    try:
        counts = library_counts()
        return {
            "libraries": [
                {"name": name, "count": counts.get(name, 0), "default_enabled": True}
                for name in ALL_LIBRARIES
                if counts.get(name, 0) > 0
            ]
        }
    except Exception as exc:
        return {"libraries": [], "error": str(exc)}


def get_catalog_stats() -> dict:
    try:
        return {
            "total_songs": count_catalog(),
            "libraries": library_counts(),
            "db": "Supabase",
            "status": "live",
        }
    except Exception as exc:
        return {"total_songs": 0, "db": "Supabase", "status": "error", "error": str(exc)}


def get_sample_songs(limit: int = 50) -> dict:
    try:
        response = (
            get_supabase()
            .table(CATALOG_VIEW)
            .select(
                "id,title,library,genre,url,tempo_bpm,energy,valence,"
                "danceability,acousticness,instrumentalness,loudness,speechness,"
                "duration_seconds,song_type"
            )
            .limit(limit)
            .execute()
        )
        return {
            "songs": [
                {
                    "id": song.get("id"),
                    "title": song.get("title", ""),
                    "library": song.get("library", ""),
                    "genre": song.get("genre", ""),
                    "url": resolve_track_src(song),
                    "tempo_bpm": song.get("tempo_bpm", 120),
                    "energy": song.get("energy", 0.5),
                    "valence": song.get("valence", 0.5),
                    "danceability": song.get("danceability", 0.5),
                    "acousticness": song.get("acousticness", 0.4),
                    "instrumentalness": song.get("instrumentalness", 0.3),
                    "loudness": song.get("loudness", -8.0),
                    "speechiness": song.get("speechness", 0.1),
                    "duration_seconds": song.get("duration_seconds", 210),
                    "song_type": song.get("song_type", ""),
                }
                for song in (response.data or [])
            ]
        }
    except Exception as exc:
        return {"songs": [], "error": str(exc)}
