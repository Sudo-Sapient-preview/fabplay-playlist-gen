import json
from datetime import datetime, timezone

from api.db import fetch_all_songs, get_supabase
from pipeline.mmr_scorer import compute_maest_sim, compute_relevance
from pipeline.rag_retriever import (
    _attach_analysis_features,
    _fetch_analysis_features,
    apply_exclusion_filters_only,
    apply_hard_filters,
)

from api.store import get_brand, get_brands
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


def _fetch_playlist_from_supabase(brand_id: str) -> dict | None:
    try:
        response = (
            get_supabase()
            .table("brand_playlists")
            .select("playlist_json")
            .eq("brand_id", brand_id)
            .order("updated_at", desc=True)
            .limit(1)
            .execute()
        )
        if response and response.data:
            raw = response.data[0]["playlist_json"]
            return json.loads(raw) if isinstance(raw, str) else raw
    except Exception:
        pass
    return None


def _sync_playlist_songs(brand_id: str, user_id: str, day_parts: list, brand_name: str = "") -> None:
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
                "brand_name": brand_name,
                "song_id": song_id,
                "song_name": song_name,
                "generated_at": now_iso,
            }
            for song_id, song_name in unique_songs.items()
        ]
        get_supabase().table("user_playlist_songs").insert(rows).execute()


def _persist_playlist_snapshot(brand_id: str, user_id: str, playlist: dict) -> None:
    playlist_id = playlist.get("playlist_id", "")
    row = {
        "brand_id": brand_id,
        "user_id": user_id,
        "playlist_json": playlist,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    if playlist_id:
        row["playlist_id"] = playlist_id
        try:
            get_supabase().table("brand_playlists").upsert(row, on_conflict="playlist_id").execute()
            return
        except Exception:
            pass
    get_supabase().table("brand_playlists").upsert(row, on_conflict="brand_id").execute()


def list_playlists_for_brand(brand_id: str) -> tuple[list | None, str | None, int | None]:
    try:
        response = (
            get_supabase()
            .table("brand_playlists")
            .select("playlist_id, playlist_name, updated_at, playlist_json")
            .eq("brand_id", brand_id)
            .order("updated_at", desc=True)
            .execute()
        )
        rows = response.data or []
        result = []
        for row in rows:
            raw = row.get("playlist_json") or {}
            pj = json.loads(raw) if isinstance(raw, str) else raw
            track_count = sum(len(dp.get("tracks", [])) for dp in pj.get("day_parts", []))
            result.append({
                "playlist_id": row.get("playlist_id") or pj.get("playlist_id", ""),
                "playlist_name": row.get("playlist_name") or pj.get("brand_profile", {}).get("brand_name", ""),
                "updated_at": row.get("updated_at", ""),
                "track_count": track_count,
                "day_parts": len(pj.get("day_parts", [])),
            })
        return result, None, None
    except Exception as exc:
        return None, str(exc), 500


def get_playlist_by_id(playlist_id: str) -> tuple[dict | None, str | None, int | None]:
    def _hydrate(raw):
        playlist = json.loads(raw) if isinstance(raw, str) else raw
        for day_part in playlist.get("day_parts", []):
            for track in day_part.get("tracks", []):
                track["src"] = resolve_track_src(track)
        return playlist

    try:
        # Primary: match on the indexed playlist_id column
        response = (
            get_supabase()
            .table("brand_playlists")
            .select("playlist_json")
            .eq("playlist_id", playlist_id)
            .maybe_single()
            .execute()
        )
        if response and response.data:
            return _hydrate(response.data["playlist_json"]), None, None
    except Exception:
        pass

    try:
        # Fallback: old records where the column was null — search inside the JSON
        response2 = (
            get_supabase()
            .table("brand_playlists")
            .select("playlist_json")
            .filter("playlist_json->>'playlist_id'", "eq", playlist_id)
            .limit(1)
            .execute()
        )
        if response2 and response2.data:
            return _hydrate(response2.data[0]["playlist_json"]), None, None
    except Exception:
        pass

    return None, "Playlist not found", 404


def _assert_brand_access(brand_id: str, user_id: str) -> tuple[dict | None, str | None, int | None]:
    brand = get_brand(brand_id)
    if not brand:
        return None, "Brand not found", 404
    if brand.get("user_id") != user_id:
        return None, "Not your brand", 403
    return brand, None, None


def get_playlist(brand_id: str) -> tuple[dict | None, str | None, int | None]:
    playlist = _fetch_playlist_from_supabase(brand_id)
    if not playlist:
        return None, "No playlist found for this brand. Run generation first.", 404

    for day_part in playlist.get("day_parts", []):
        for track in day_part.get("tracks", []):
            track["src"] = resolve_track_src(track)
    return playlist, None, None


def remove_tracks(brand_id: str, user_id: str, day_part_index: int, song_ids: list[str]) -> tuple[dict | None, str | None, int | None]:
    brand, message, status = _assert_brand_access(brand_id, user_id)
    if message:
        return None, message, status
    brand_name = (brand or {}).get("brand_name", "")

    playlist = _fetch_playlist_from_supabase(brand_id)
    if not playlist:
        return None, "No playlist found", 404
    if not song_ids:
        return None, "No song_ids provided", 400

    day_parts = playlist.get("day_parts", [])
    if day_part_index < 0 or day_part_index >= len(day_parts):
        return None, "Invalid day_part_index", 400

    day_part = day_parts[day_part_index]
    remove_set = set(song_ids)
    day_part["tracks"] = [track for track in day_part.get("tracks", []) if track.get("song_id") not in remove_set]
    _recalc_daypart_stats(day_part)

    try:
        _persist_playlist_snapshot(brand_id, user_id, playlist)
    except Exception:
        pass
    try:
        _sync_playlist_songs(brand_id, user_id, day_parts, brand_name)
    except Exception:
        pass

    for track in day_part.get("tracks", []):
        track["src"] = resolve_track_src(track)
    return {"ok": True, "day_part": day_part}, None, None


def suggest_tracks(brand_id: str, user_id: str, day_part_index: int, song_id: str, top_k: int = 8, playlist_id: str = "") -> tuple[dict | None, str | None, int | None]:
    brand, message, status = _assert_brand_access(brand_id, user_id)
    if message:
        return None, message, status

    playlist = None
    if playlist_id:
        playlist, _msg, _st = get_playlist_by_id(playlist_id)
    if not playlist:
        playlist = _fetch_playlist_from_supabase(brand_id)
    if not playlist:
        return None, "No playlist found", 404

    day_parts = playlist.get("day_parts", [])
    if day_part_index < 0 or day_part_index >= len(day_parts):
        return None, "Invalid day_part_index", 400

    day_part = day_parts[day_part_index]
    tracks = day_part.get("tracks", [])
    if not any(str(t.get("song_id")) == str(song_id) for t in tracks):
        return None, "Track not found in day-part", 404

    used_ids = {str(t.get("song_id", "")) for dp in day_parts for t in dp.get("tracks", [])}

    all_songs = fetch_all_songs()
    for s in all_songs:
        if "song_id" not in s:
            s["song_id"] = s.get("id", "")

    query_song = next((s for s in all_songs if str(s["song_id"]) == str(song_id)), None)
    if not query_song:
        return None, "Song not found in catalog", 404

    inputs = pipeline_inputs(brand)
    candidates = apply_exclusion_filters_only(all_songs, inputs)
    candidates = [s for s in candidates if str(s["song_id"]) not in used_ids]
    if not candidates:
        return None, "No suggestions available", 404

    pre_ranked = sorted(candidates, key=lambda c: compute_maest_sim(c, query_song), reverse=True)
    top_candidates = pre_ranked[:200]

    analysis_ids = [s["song_id"] for s in top_candidates] + [song_id]
    analysis = _fetch_analysis_features(analysis_ids, include_maest=True)
    if analysis:
        _attach_analysis_features(top_candidates, analysis)
        q_feat = analysis.get(str(song_id))
        if q_feat:
            if q_feat.get("maest_audio_768"):
                query_song["maest_audio_768"] = q_feat["maest_audio_768"]
            if q_feat.get("clap_audio_512"):
                query_song["clap_audio_512"] = q_feat["clap_audio_512"]
            # arousal and mood_predicted_labels come from songs table, already on query_song

    final_ranked = sorted(top_candidates, key=lambda c: compute_maest_sim(c, query_song), reverse=True)
    top = final_ranked[:top_k]

    new_tracks = []
    for song in top:
        song["relevance_score"] = compute_maest_sim(song, query_song)
        track = fmt_track(song)
        track["suggested"] = True
        track["src"] = resolve_track_src(track)
        new_tracks.append(track)

    return {"suggestions": new_tracks}, None, None


def add_suggested_tracks(brand_id: str, user_id: str, day_part_index: int, song_ids: list[str], playlist_id: str = "") -> tuple[dict | None, str | None, int | None]:
    brand, message, status = _assert_brand_access(brand_id, user_id)
    if message:
        return None, message, status
    brand_name = (brand or {}).get("brand_name", "")

    playlist = None
    if playlist_id:
        playlist, _msg, _st = get_playlist_by_id(playlist_id)
    if not playlist:
        playlist = _fetch_playlist_from_supabase(brand_id)
    if not playlist:
        return None, "No playlist found", 404
    if not song_ids:
        return None, "No song_ids provided", 400

    day_parts = playlist.get("day_parts", [])
    if day_part_index < 0 or day_part_index >= len(day_parts):
        return None, "Invalid day_part_index", 400

    day_part = day_parts[day_part_index]
    existing_ids = {str(t.get("song_id", "")) for dp in day_parts for t in dp.get("tracks", [])}
    new_ids = [sid for sid in song_ids if str(sid) not in existing_ids]

    if new_ids:
        all_songs = fetch_all_songs()
        for s in all_songs:
            if "song_id" not in s:
                s["song_id"] = s.get("id", "")
        id_to_song = {str(s["song_id"]): s for s in all_songs}

        new_tracks = []
        for sid in new_ids:
            song = id_to_song.get(str(sid))
            if not song:
                continue
            track = fmt_track(song)
            track["suggested"] = True
            new_tracks.append(track)

        day_part["tracks"].extend(new_tracks)
        _recalc_daypart_stats(day_part)

        try:
            _persist_playlist_snapshot(brand_id, user_id, playlist)
        except Exception:
            pass
        try:
            _sync_playlist_songs(brand_id, user_id, day_parts, brand_name)
        except Exception:
            pass

    for track in day_part.get("tracks", []):
        track["src"] = resolve_track_src(track)
    return {"ok": True, "added": len(new_ids), "day_part": day_part}, None, None


def replace_track(brand_id: str, user_id: str, day_part_index: int, song_id: str) -> tuple[dict | None, str | None, int | None]:
    brand, message, status = _assert_brand_access(brand_id, user_id)
    if message:
        return None, message, status
    brand_name = (brand or {}).get("brand_name", "")

    playlist = _fetch_playlist_from_supabase(brand_id)
    if not playlist:
        return None, "No playlist found", 404
    if not song_id:
        return None, "song_id required", 400

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
    analysis = _fetch_analysis_features(all_ids, include_maest=True)
    if analysis:
        _attach_analysis_features(candidates, analysis)
        original_features = analysis.get(str(original.get("song_id", "")))
        if original_features:
            if original_features.get("maest_audio_768"):
                original["maest_audio_768"] = original_features["maest_audio_768"]
            if original_features.get("clap_audio_512"):
                original["clap_audio_512"] = original_features["clap_audio_512"]
            # arousal and mood_predicted_labels come from songs table, already on original

    def score(candidate: dict) -> float:
        return 0.6 * compute_relevance(candidate, day_part) + 0.4 * compute_maest_sim(candidate, original)

    best = max(candidates, key=score)
    best.setdefault("song_id", best.get("id", ""))
    best["relevance_score"] = score(best)
    replacement = fmt_track(best)
    tracks[track_index] = replacement
    day_part["tracks"] = tracks
    _recalc_daypart_stats(day_part)

    try:
        _persist_playlist_snapshot(brand_id, user_id, playlist)
    except Exception:
        pass
    try:
        _sync_playlist_songs(brand_id, user_id, day_parts, brand_name)
    except Exception:
        pass

    for track in day_part.get("tracks", []):
        track["src"] = resolve_track_src(track)
    return {"ok": True, "replacement": replacement, "day_part": day_part}, None, None
