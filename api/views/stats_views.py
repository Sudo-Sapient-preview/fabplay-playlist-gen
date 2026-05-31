import json

from django.views.decorators.http import require_GET

from api.activity import get_activity
from api.auth import get_user_role, require_auth
from api.db import get_supabase
from api.services.brand_service import list_brands_for_user
from api.store import get_brands
from api.utils import ok, time_ago


@require_GET
@require_auth
def stats(_request):
    brands = get_brands()
    active = sum(1 for brand in brands.values() if brand.get("status") == "active")
    try:
        playlist_rows = get_supabase().table("brand_playlists").select("playlist_json").execute().data or []
    except Exception:
        playlist_rows = []
    def _parse_playlist_json(raw):
        if isinstance(raw, dict):
            return raw
        if isinstance(raw, str):
            try:
                return json.loads(raw)
            except Exception:
                return {}
        return {}

    bfs_values = [
        day_part["avg_bfs"]
        for row in playlist_rows
        for day_part in _parse_playlist_json(row.get("playlist_json")).get("day_parts", [])
        if day_part.get("avg_bfs", 0) > 0
    ]
    return ok(
        {
            "total_brands": len(brands),
            "active_sound_boards": active,
            "playlists_this_month": len(playlist_rows),
            "avg_bfs": round(sum(bfs_values) / len(bfs_values), 3) if bfs_values else 0.0,
        }
    )


@require_GET
@require_auth
def activity(request):
    _ = request.user_data
    return ok([{**item, "time_ago": time_ago(item["timestamp"])} for item in get_activity()[:10]])


@require_GET
@require_auth
def init(request):
    user = request.user_data
    role = get_user_role(user["id"])
    request.user_role = role
    brands = list_brands_for_user(user["id"])
    return ok(
        {
            "user": {"id": user["id"], "email": user.get("email", ""), "role": role},
            "brands": brands,
        }
    )
