import json

from django.views.decorators.http import require_GET, require_http_methods

from api import analytics
from api.auth import require_auth
from api.services.brand_service import (
    create_brand,
    delete_brand,
    list_brands_for_user,
    rename_playlist,
    update_dayparts,
    update_genres,
    update_profile,
    update_soundboard_targets,
)
from api.utils import err, ok


def _json_body(request) -> tuple[dict | None, str | None]:
    try:
        return json.loads(request.body.decode("utf-8") or "{}"), None
    except json.JSONDecodeError:
        return None, "Invalid JSON body"


@require_http_methods(["GET", "POST"])
@require_auth
def brands_collection(request):
    if request.method == "GET":
        return ok(list_brands_for_user(request.user_data["id"]))

    payload, error_message = _json_body(request)
    if error_message:
        return err(error_message, 400)

    brand, service_error = create_brand(request.user_data["id"], payload or {})
    if service_error:
        return err(service_error, 400)
    user_id = request.user_data["id"]
    analytics.capture(
        user_id,
        "brand_created",
        {
            "brand_id": brand.get("id") if isinstance(brand, dict) else None,
            "brand_name": (brand or {}).get("brand_name") if isinstance(brand, dict) else None,
            "category": (brand or {}).get("category") if isinstance(brand, dict) else None,
        },
    )
    return ok(brand)


@require_http_methods(["DELETE"])
@require_auth
def delete_brand_view(request, brand_id: str):
    success, message, status = delete_brand(brand_id, request.user_data["id"])
    if not success:
        return err(message or "Delete failed", status or 400)
    analytics.capture(
        request.user_data["id"],
        "brand_deleted",
        {"brand_id": brand_id},
    )
    return ok({"ok": True})


@require_http_methods(["PATCH"])
@require_auth
def rename_playlist_view(request, brand_id: str):
    payload, error_message = _json_body(request)
    if error_message:
        return err(error_message, 400)

    success, message, status = rename_playlist(
        brand_id,
        request.user_data["id"],
        (payload or {}).get("name", ""),
    )
    if not success:
        return err(message or "Rename failed", status or 400)
    return ok({"ok": True})


@require_http_methods(["PUT"])
@require_auth
def update_soundboard_view(request, brand_id: str):
    payload, error_message = _json_body(request)
    if error_message:
        return err(error_message, 400)
    success, message, status = update_soundboard_targets(
        brand_id, request.user_data["id"], (payload or {}).get("targets", {})
    )
    if not success:
        return err(message or "Save failed", status or 400)
    return ok({"ok": True})


@require_http_methods(["PUT"])
@require_auth
def update_dayparts_view(request, brand_id: str):
    payload, error_message = _json_body(request)
    if error_message:
        return err(error_message, 400)
    success, message, status = update_dayparts(
        brand_id, request.user_data["id"], (payload or {}).get("day_parts", [])
    )
    if not success:
        return err(message or "Save failed", status or 400)
    return ok({"ok": True})


@require_http_methods(["PUT"])
@require_auth
def update_profile_view(request, brand_id: str):
    payload, error_message = _json_body(request)
    if error_message:
        return err(error_message, 400)
    success, message, status = update_profile(
        brand_id, request.user_data["id"], payload or {}
    )
    if not success:
        return err(message or "Save failed", status or 400)
    return ok({"ok": True})


@require_http_methods(["PUT"])
@require_auth
def update_genres_view(request, brand_id: str):
    payload, error_message = _json_body(request)
    if error_message:
        return err(error_message, 400)
    success, message, status = update_genres(
        brand_id,
        request.user_data["id"],
        (payload or {}).get("include_genres"),
        (payload or {}).get("exclude_genres"),
        (payload or {}).get("include_song_types"),
        (payload or {}).get("libraries"),
    )
    if not success:
        return err(message or "Save failed", status or 400)
    return ok({"ok": True})
