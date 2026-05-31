import json

from django.views.decorators.http import require_GET, require_http_methods

from api.auth import require_auth
from api.services.playlist_service import add_suggested_tracks, get_playlist, get_playlist_by_id, list_playlists_for_brand, remove_tracks, replace_track, suggest_tracks
from api.utils import err, ok


def _json_body(request) -> tuple[dict | None, str | None]:
    try:
        return json.loads(request.body.decode("utf-8") or "{}"), None
    except json.JSONDecodeError:
        return None, "Invalid JSON body"


@require_GET
@require_auth
def get_playlist_view(_request, brand_id: str):
    playlist, message, status = get_playlist(brand_id)
    if not playlist:
        return err(message or "Playlist not found", status or 404)
    return ok(playlist)


@require_GET
@require_auth
def list_brand_playlists_view(_request, brand_id: str):
    playlists, message, status = list_playlists_for_brand(brand_id)
    if playlists is None:
        return err(message or "Failed to load playlists", status or 500)
    return ok(playlists)


@require_GET
@require_auth
def get_playlist_by_id_view(_request, playlist_id: str):
    playlist, message, status = get_playlist_by_id(playlist_id)
    if not playlist:
        return err(message or "Playlist not found", status or 404)
    return ok(playlist)


@require_http_methods(["DELETE"])
@require_auth
def remove_tracks_view(request, brand_id: str):
    payload, error_message = _json_body(request)
    if error_message:
        return err(error_message, 400)
    payload = payload or {}
    result, message, status = remove_tracks(
        brand_id,
        request.user_data["id"],
        int(payload.get("day_part_index", -1)),
        payload.get("song_ids", []),
    )
    if not result:
        return err(message or "Remove failed", status or 400)
    return ok(result)


@require_http_methods(["POST"])
@require_auth
def suggest_tracks_view(request, brand_id: str):
    payload, error_message = _json_body(request)
    if error_message:
        return err(error_message, 400)
    payload = payload or {}
    result, message, status = suggest_tracks(
        brand_id,
        request.user_data["id"],
        int(payload.get("day_part_index", -1)),
        payload.get("song_id", ""),
        int(payload.get("top_k", 8)),
        playlist_id=payload.get("playlist_id", ""),
    )
    if not result:
        return err(message or "Suggest failed", status or 400)
    return ok(result)


@require_http_methods(["POST"])
@require_auth
def add_suggested_tracks_view(request, brand_id: str):
    payload, error_message = _json_body(request)
    if error_message:
        return err(error_message, 400)
    payload = payload or {}
    result, message, status = add_suggested_tracks(
        brand_id,
        request.user_data["id"],
        int(payload.get("day_part_index", -1)),
        payload.get("song_ids", []),
        playlist_id=payload.get("playlist_id", ""),
    )
    if not result:
        return err(message or "Save failed", status or 400)
    return ok(result)


@require_http_methods(["POST"])
@require_auth
def replace_track_view(request, brand_id: str):
    payload, error_message = _json_body(request)
    if error_message:
        return err(error_message, 400)
    payload = payload or {}
    result, message, status = replace_track(
        brand_id,
        request.user_data["id"],
        int(payload.get("day_part_index", -1)),
        payload.get("song_id", ""),
    )
    if not result:
        return err(message or "Replace failed", status or 400)
    return ok(result)
