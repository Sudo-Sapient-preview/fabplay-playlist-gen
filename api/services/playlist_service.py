import json
from datetime import datetime, timezone

from api.db import fetch_all_songs, get_supabase
from pipeline.mmr_scorer import compute_relevance, compute_track_sim
from pipeline.rag_retriever import (
    _attach_analysis_features,
    _fetch_analysis_features,
    apply_exclusion_filters_only,
    apply_hard_filters,
)

from api.store import get_brand, get_brands, get_playlists, save_playlists
from api.utils import fmt_track, pipeline_inputs, resolve_track_src


def _recalc_daypart_stats(day_part: dict) -> dict:
    tracks = day_part.get("tracks", [])
    day_part["track_count"] = len(tracks)
    day_part["total_duration_seconds"] = sum(track.get("duration_seconds", 0) for track in tracks)
    if tracks:
        day_part["avg_bfs"] = round(sum(track.get("bfs", 0) for track in tracks) / len(tracks), 3)
        day_part["avg_mmr"] = round(sum(track.get("mmr_score", 0) for track in tracks) / len(tracks), 3)
    else:
        day_part["avg_bfs"] = 0.0
        day_part["avg_mmr"] = 0.0
    return day_part


def _sync_playlist_songs(brand_id: str, user_id: str, day_parts: list) -> None:
    unique_songs: dict[str, str] = {}
    for day_part in day_parts:
        for track in day_part.get("tracks", []):
            song_id = track.get("song_id")
            if song_id and song_id not in unique_songs:
                unique_songs[song_id] = track.get("title", "")
    now_iso = datetime.now(timezone.utc).isoformat()
    get_supabase().table("user_playlist_songs").delete().eq("user_id", user_id).eq("brand_id", brand_id).execute()
    if unique_songs:
        rows = [
            {
                "user_id": user_id,
                "brand_id": brand_id,
                "song_id": song_id,
                "song_name": song_name,
                "generated_at": now_iso,
            }
            for song_id, song_name in unique_songs.items()
        ]
        get_supabase().table("user_playlist_songs").insert(rows).execute()


def _persist_playlist_snapshot(brand_id: str, user_id: str, playlist: dict) -> None:
    get_supabase().table("brand_playlists").upsert(
        {
            "brand_id": brand_id,
            "user_id": user_id,
            "playlist_json": playlist,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        },
        on_conflict="brand_id",
    ).execute()


def _assert_brand_access(brand_id: str, user_id: str) -> tuple[dict | None, str | None, int | None]:
    brand = get_brand(brand_id)
    if not brand:
        return None, "Brand not found", 404
    if brand.get("user_id") != user_id:
        return None, "Not your brand", 403
    return brand, None, None


def get_playlist(brand_id: str) -> tuple[dict | None, str | None, int | None]:
    playlist = get_playlists().get(brand_id)
    if not playlist:
        try:
            response = (
                get_supabase()
                .table("brand_playlists")
                .select("playlist_json")
                .eq("brand_id", brand_id)
                .maybe_single()
                .execute()
            )
            if response and response.data:
                raw = response.data["playlist_json"]
                playlist = json.loads(raw) if isinstance(raw, str) else raw
                playlists = get_playlists()
                playlists[brand_id] = playlist
                save_playlists(playlists)
        except Exception:
            playlist = None
    if not playlist:
        return None, "No playlist found for this brand. Run generation first.", 404

    for day_part in playlist.get("day_parts", []):
        for track in day_part.get("tracks", []):
            track["src"] = resolve_track_src(track)
    return playlist, None, None


def remove_tracks(brand_id: str, user_id: str, day_part_index: int, song_ids: list[str]) -> tuple[dict | None, str | None, int | None]:
    _, message, status = _assert_brand_access(brand_id, user_id)
    if message:
        return None, message, status

    playlists = get_playlists()
    if brand_id not in playlists:
        return None, "No playlist found", 404
    if not song_ids:
        return None, "No song_ids provided", 400

    playlist = playlists[brand_id]
    day_parts = playlist.get("day_parts", [])
    if day_part_index < 0 or day_part_index >= len(day_parts):
        return None, "Invalid day_part_index", 400

    day_part = day_parts[day_part_index]
    remove_set = set(song_ids)
    day_part["tracks"] = [track for track in day_part.get("tracks", []) if track.get("song_id") not in remove_set]
    _recalc_daypart_stats(day_part)
    save_playlists(playlists)

    try:
        _sync_playlist_songs(brand_id, user_id, day_parts)
    except Exception:
        pass
    try:
        _persist_playlist_snapshot(brand_id, user_id, playlist)
    except Exception:
        pass

    for track in day_part.get("tracks", []):
        track["src"] = resolve_track_src(track)
    return {"ok": True, "day_part": day_part}, None, None


def suggest_tracks(brand_id: str, user_id: str, day_part_index: int, song_id: str, top_k: int = 8) -> tuple[dict | None, str | None, int | None]:
    _, message, status = _assert_brand_access(brand_id, user_id)
    if message:
        return None, message, status

    playlists = get_playlists()
    if brand_id not in playlists:
        return None, "No playlist found", 404

    playlist = playlists[brand_id]
    day_parts = playlist.get("day_parts", [])
    if day_part_index < 0 or day_part_index >= len(day_parts):
        return None, "Invalid day_part_index", 400

    day_part = day_parts[day_part_index]
    tracks = day_part.get("tracks", [])
    if not any(t.get("song_id") == song_id for t in tracks):
        return None, "Track not found in day-part", 404

    used_ids = {t.get("song_id") for dp in day_parts for t in dp.get("tracks", [])}

    all_songs = fetch_all_songs()
    for s in all_songs:
        if "song_id" not in s:
            s["song_id"] = s.get("id", "")

    query_song = next((s for s in all_songs if s["song_id"] == song_id), None)
    if not query_song:
        return None, "Song not found in catalog", 404

    inputs = pipeline_inputs(get_brands()[brand_id])
    candidates = apply_exclusion_filters_only(all_songs, inputs)
    candidates = [s for s in candidates if s["song_id"] not in used_ids]
    if not candidates:
        return None, "No suggestions available", 404

    # Pre-rank by feature-vector similarity, then fetch CLAP for top 200
    pre_ranked = sorted(candidates, key=lambda c: compute_track_sim(c, query_song), reverse=True)
    top_candidates = pre_ranked[:200]

    analysis_ids = [s["song_id"] for s in top_candidates] + [song_id]
    analysis = _fetch_analysis_features(analysis_ids)
    if analysis:
        _attach_analysis_features(top_candidates, analysis)
        q_feat = analysis.get(song_id)
        if q_feat:
            if q_feat.get("clap_audio_512"):
                query_song["clap_audio_512"] = q_feat["clap_audio_512"]
            if q_feat.get("mood_predicted_labels") is not None:
                query_song["mood_predicted_labels"] = q_feat["mood_predicted_labels"]
            if q_feat.get("arousal") is not None:
                query_song["arousal"] = q_feat["arousal"]

    final_ranked = sorted(top_candidates, key=lambda c: compute_track_sim(c, query_song), reverse=True)
    top = final_ranked[:top_k]

    new_tracks = []
    for song in top:
        song["relevance_score"] = compute_track_sim(song, query_song)
        track = fmt_track(song)
        track["suggested"] = True
        new_tracks.append(track)

    day_part["tracks"].extend(new_tracks)
    _recalc_daypart_stats(day_part)
    save_playlists(playlists)

    try:
        _sync_playlist_songs(brand_id, user_id, day_parts)
    except Exception:
        pass
    try:
        _persist_playlist_snapshot(brand_id, user_id, playlist)
    except Exception:
        pass

    for track in day_part.get("tracks", []):
        track["src"] = resolve_track_src(track)
    return {"ok": True, "added": len(new_tracks), "day_part": day_part}, None, None


def replace_track(brand_id: str, user_id: str, day_part_index: int, song_id: str) -> tuple[dict | None, str | None, int | None]:
    _, message, status = _assert_brand_access(brand_id, user_id)
    if message:
        return None, message, status

    playlists = get_playlists()
    if brand_id not in playlists:
        return None, "No playlist found", 404
    if not song_id:
        return None, "song_id required", 400

    playlist = playlists[brand_id]
    day_parts = playlist.get("day_parts", [])
    if day_part_index < 0 or day_part_index >= len(day_parts):
        return None, "Invalid day_part_index", 400

    day_part = day_parts[day_part_index]
    tracks = day_part.get("tracks", [])
    track_index = next((index for index, track in enumerate(tracks) if track.get("song_id") == song_id), None)
    if track_index is None:
        return None, "Track not found in day-part", 404

    original = tracks[track_index]
    used_ids = {track.get("song_id") for dp in day_parts for track in dp.get("tracks", [])}
    all_songs = fetch_all_songs()
    inputs = pipeline_inputs(get_brands()[brand_id])
    filtered = apply_hard_filters(all_songs, day_part, inputs)
    if not filtered:
        filtered = apply_exclusion_filters_only(all_songs, inputs)
    candidates = [
        song
        for song in filtered
        if song.get("song_id", song.get("id", "")) not in used_ids
    ]
    if not candidates:
        return None, "No replacement candidates available", 404

    all_ids = [str(song.get("song_id", song.get("id", ""))) for song in candidates] + [str(original.get("song_id", ""))]
    analysis = _fetch_analysis_features(all_ids)
    if analysis:
        _attach_analysis_features(candidates, analysis)
        original_features = analysis.get(str(original.get("song_id", "")))
        if original_features:
            if original_features.get("clap_audio_512"):
                original["clap_audio_512"] = original_features["clap_audio_512"]
            if original_features.get("mood_predicted_labels") is not None:
                original["mood_predicted_labels"] = original_features["mood_predicted_labels"]
            if original_features.get("arousal") is not None:
                original["arousal"] = original_features["arousal"]

    def score(candidate: dict) -> float:
        return 0.6 * compute_relevance(candidate, day_part) + 0.4 * compute_track_sim(candidate, original)

    best = max(candidates, key=score)
    best.setdefault("song_id", best.get("id", ""))
    best["relevance_score"] = score(best)
    replacement = fmt_track(best)
    tracks[track_index] = replacement
    day_part["tracks"] = tracks
    _recalc_daypart_stats(day_part)
    save_playlists(playlists)

    try:
        _sync_playlist_songs(brand_id, user_id, day_parts)
    except Exception:
        pass
    try:
        _persist_playlist_snapshot(brand_id, user_id, playlist)
    except Exception:
        pass

    for track in day_part.get("tracks", []):
        track["src"] = resolve_track_src(track)
    return {"ok": True, "replacement": replacement, "day_part": day_part}, None, None
