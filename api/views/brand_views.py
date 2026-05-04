import json

from django.views.decorators.http import require_GET, require_http_methods

from api.auth import require_auth
from api.services.brand_service import (
    create_brand,
    delete_brand,
    list_brands_for_user,
    rename_playlist,
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
    return ok(brand)


@require_http_methods(["DELETE"])
@require_auth
def delete_brand_view(request, brand_id: str):
    success, message, status = delete_brand(brand_id, request.user_data["id"])
    if not success:
        return err(message or "Delete failed", status or 400)
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
