from datetime import datetime, timezone

from django.conf import settings
from django.http import JsonResponse


def clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def ok(data: dict | list, status: int = 200) -> JsonResponse:
    return JsonResponse(data, status=status, safe=not isinstance(data, list))


def err(message: str, status: int = 400, **extra) -> JsonResponse:
    payload = {"error": message}
    payload.update(extra)
    return JsonResponse(payload, status=status)


def resolve_track_src(track: dict) -> str:
    raw = str(track.get("url") or track.get("src") or "").strip()
    if not raw:
        return ""
    if raw.startswith(("http://", "https://", "data:", "blob:")):
        return raw
    rel = raw.lstrip("/")
    if settings.SONGS_BASE_URL:
        return f"{settings.SONGS_BASE_URL}/{rel}"
    if rel.startswith("songs/"):
        rel = rel[len("songs/") :]
    return f"/songs/{rel}"


def time_ago(ts: str) -> str:
    try:
        diff = int(
            (datetime.now(timezone.utc) - datetime.fromisoformat(ts)).total_seconds()
        )
        if diff < 60:
            return f"{diff}s ago"
        if diff < 3600:
            return f"{diff // 60}m ago"
        if diff < 86400:
            return f"{diff // 3600}h ago"
        return f"{diff // 86400}d ago"
    except Exception:
        return "just now"


def fmt_track(track: dict) -> dict:
    return {
        "song_id": track.get("song_id", track.get("id", "")),
        "title": track.get("title", ""),
        "artist": track.get("artist", ""),
        "genre": track.get("genre", ""),
        "url": track.get("url", ""),
        "src": track.get("url") or track.get("src", ""),
        "bpm": round(float(track.get("tempo_bpm", 120))),
        "bfs": round(
            float(track.get("relevance_score", track.get("mmr_score", 0.5))),
            3,
        ),
        "mmr_score": round(float(track.get("mmr_score", 0.5)), 3),
        "energy": round(float(track.get("energy", 0.5)), 3),
        "valence": round(float(track.get("valence", 0.5)), 3),
        "danceability": round(float(track.get("danceability", 0.5)), 3),
        "acousticness": round(float(track.get("acousticness", 0.4)), 3),
        "instrumentalness": round(float(track.get("instrumentalness", 0.3)), 3),
        "loudness": round(float(track.get("loudness", -8.0)), 3),
        "speechiness": round(float(track.get("speechness", 0.1)), 3),
        "duration_seconds": round(float(track.get("duration_seconds", 210))),
    }


def fmt_daypart(day_part: dict, playlist: list[dict]) -> dict:
    tracks = [fmt_track(track) for track in playlist]
    duration = sum(track["duration_seconds"] for track in tracks)
    avg_bfs = (
        round(sum(track["bfs"] for track in tracks) / len(tracks), 3) if tracks else 0.0
    )
    avg_mmr = (
        round(sum(track["mmr_score"] for track in tracks) / len(tracks), 3)
        if tracks
        else 0.0
    )
    return {
        "name": day_part.get("name", ""),
        "start_time": day_part.get("start_time", ""),
        "end_time": day_part.get("end_time", ""),
        "character_description": day_part.get(
            "character_description",
            day_part.get("character", ""),
        ),
        "genre_emphasis": day_part.get("genre_emphasis", []),
        "energy_target": day_part.get("energy_target", 0.5),
        "energy_min": day_part.get("energy_min", 0.0),
        "energy_max": day_part.get("energy_max", 1.0),
        "valence_target": day_part.get("valence_target", 0.5),
        "valence_min": day_part.get("valence_min", 0.0),
        "valence_max": day_part.get("valence_max", 1.0),
        "tempo_target": day_part.get("tempo_target", 110),
        "tempo_min": day_part.get("tempo_min", 60),
        "tempo_max": day_part.get("tempo_max", 180),
        "danceability_target": day_part.get("danceability_target", 0.5),
        "danceability_min": day_part.get("danceability_min", 0.0),
        "danceability_max": day_part.get("danceability_max", 1.0),
        "acousticness_target": day_part.get("acousticness_target", 0.4),
        "acousticness_min": day_part.get("acousticness_min", 0.0),
        "acousticness_max": day_part.get("acousticness_max", 1.0),
        "instrumentalness_target": day_part.get("instrumentalness_target", 0.3),
        "instrumentalness_min": day_part.get("instrumentalness_min", 0.0),
        "instrumentalness_max": day_part.get("instrumentalness_max", 1.0),
        "loudness_target": day_part.get("loudness_target", 0.5),
        "loudness_min": day_part.get("loudness_min", 0.0),
        "loudness_max": day_part.get("loudness_max", 1.0),
        "speechiness_target": day_part.get("speechiness_target", 0.2),
        "speechiness_min": day_part.get("speechiness_min", 0.0),
        "speechiness_max": day_part.get("speechiness_max", 1.0),
        "track_count": len(tracks),
        "total_duration_seconds": round(duration),
        "avg_bfs": avg_bfs,
        "avg_mmr": avg_mmr,
        "tracks": tracks,
    }


def pipeline_inputs(brand: dict) -> dict:
    include_genres = brand.get("include_genres", [])
    exclude_genres = brand.get("exclude_genres", [])
    include_artists = brand.get("include_artists", [])
    exclude_artists = brand.get("exclude_artists", [])

    def as_str(value):
        if isinstance(value, list):
            return ", ".join(value)
        return str(value) if value else ""

    return {
        "brand_name": brand.get("brand_name", ""),
        "business_category": brand.get("category", ""),
        "website_url": brand.get("website_url", ""),
        "brand_description": brand.get("brand_description", ""),
        "customer_description": brand.get("customer_description", ""),
        "customer_segment": brand.get("customer_segment", "mid_range"),
        "age_min": brand.get("age_min"),
        "age_max": brand.get("age_max"),
        "lifestyle_tags": brand.get("lifestyle_tags", []),
        "include_genres": as_str(include_genres),
        "exclude_genres": as_str(exclude_genres),
        "include_artists": as_str(include_artists),
        "exclude_artists": as_str(exclude_artists),
        "filter_explicit": brand.get("filter_explicit", True),
        "music_notes": brand.get("music_notes", ""),
        "asset_analysis": brand.get("asset_analysis", ""),
        "has_brand_guidelines": brand.get("has_brand_guidelines", False),
    }
