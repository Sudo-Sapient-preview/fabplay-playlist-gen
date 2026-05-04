from django.views.decorators.http import require_GET

from api.activity import get_activity
from api.auth import get_user_role, require_auth
from api.store import get_brands, get_playlists
from api.utils import ok, time_ago


@require_GET
@require_auth
def stats(_request):
    brands = get_brands()
    playlists = get_playlists()
    active = sum(1 for brand in brands.values() if brand.get("status") == "active")
    bfs_values = [
        day_part["avg_bfs"]
        for playlist in playlists.values()
        for day_part in playlist.get("day_parts", [])
        if day_part.get("avg_bfs", 0) > 0
    ]
    return ok(
        {
            "total_brands": len(brands),
            "active_sound_boards": active,
            "playlists_this_month": len(playlists),
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
    brands = [brand for brand in get_brands().values() if brand.get("user_id") == user["id"]]
    return ok(
        {
            "user": {"id": user["id"], "email": user.get("email", ""), "role": role},
            "brands": brands,
        }
    )
