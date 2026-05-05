import json

from django.views.decorators.http import require_GET, require_http_methods

from api.auth import require_auth
from api.services.playlist_service import get_playlist, remove_tracks, replace_track, suggest_tracks
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
    )
    if not result:
        return err(message or "Suggest failed", status or 400)
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
