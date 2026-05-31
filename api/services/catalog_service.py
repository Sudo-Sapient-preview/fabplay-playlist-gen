from api.db import fetch_all_songs, get_supabase


def get_genres() -> dict:
    try:
        songs = fetch_all_songs()
        genres = sorted({song.get("genre") for song in songs if song.get("genre")})
        return {"genres": genres}
    except Exception as exc:
        return {"genres": [], "error": str(exc)}


def get_artists() -> dict:
    try:
        songs = fetch_all_songs()
        artists = sorted({song.get("artist") for song in songs if song.get("artist")})
        return {"artists": artists}
    except Exception as exc:
        return {"artists": [], "error": str(exc)}


def get_catalog_stats() -> dict:
    try:
        response = get_supabase().table("songs").select("id", count="exact").execute()
        return {"total_songs": response.count or 0, "db": "Supabase", "status": "live"}
    except Exception as exc:
        return {"total_songs": 0, "db": "Supabase", "status": "error", "error": str(exc)}


def get_sample_songs(limit: int = 50) -> dict:
    try:
        response = (
            get_supabase()
            .table("songs")
            .select(
                "id,title,artist,genre,url,tempo_bpm,energy,valence,"
                "danceability,acousticness,instrumentalness,loudness,speechness,duration_seconds,song_type"
            )
            .limit(limit)
            .execute()
        )
        return {
            "songs": [
                {
                    "id": song.get("id"),
                    "title": song.get("title", ""),
                    "artist": song.get("artist", ""),
                    "genre": song.get("genre", ""),
                    "url": song.get("url", ""),
                    "tempo_bpm": song.get("tempo_bpm", 120),
                    "energy": song.get("energy", 0.5),
                    "valence": song.get("valence", 0.5),
                    "danceability": song.get("danceability", 0.5),
                    "acousticness": song.get("acousticness", 0.4),
                    "instrumentalness": song.get("instrumentalness", 0.3),
                    "loudness": song.get("loudness", -8.0),
                    "speechiness": song.get("speechness", 0.1),
                    "duration_seconds": song.get("duration_seconds", 210),
                }
                for song in (response.data or [])
            ]
        }
    except Exception as exc:
        return {"songs": [], "error": str(exc)}
