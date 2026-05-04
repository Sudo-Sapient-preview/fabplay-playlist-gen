import json
import os

import httpx
from django.views.decorators.http import require_GET, require_http_methods

from api.db import get_supabase
from api.auth import require_superadmin
from api.utils import err, ok


SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY", "")


@require_GET
@require_superadmin
def debug_search(_request):
    try:
        zero_vec = "[" + ",".join(["0.0"] * 1536) + "]"
        result = get_supabase().rpc(
            "search_songs_by_embedding",
            {"query_embedding": zero_vec, "match_count": 5, "min_similarity": 0.0},
        ).execute()
        error = getattr(result, "error", None)
        rows = result.data or []
        return ok(
            {
                "rpc_exists": True,
                "error": str(error) if error else None,
                "rows_returned": len(rows),
                "sample": rows[:2],
                "song_embeddings_count": get_supabase()
                .table("song_embeddings")
                .select("id", count="exact")
                .execute()
                .count,
            }
        )
    except Exception as exc:
        return ok({"rpc_exists": False, "error": str(exc)})


@require_GET
@require_superadmin
def debug_songs(_request):
    try:
        response = get_supabase().table("songs").select("id,title,artist,url").limit(3).execute()
        sample = [
            {"id": song["id"], "title": song.get("title", ""), "url": song.get("url", "")}
            for song in (response.data or [])
        ]
        total = get_supabase().table("songs").select("id", count="exact").execute().count or 0
    except Exception as exc:
        return ok({"error": str(exc)})
    return ok({"total_songs": total, "sample": sample})


@require_GET
@require_superadmin
def list_users(_request):
    with httpx.Client(timeout=10.0) as client:
        response = client.get(
            f"{SUPABASE_URL}/auth/v1/admin/users?per_page=1000",
            headers={
                "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
                "apikey": SUPABASE_SERVICE_KEY,
            },
        )
    if response.status_code != 200:
        return err("Could not fetch users from Supabase", 500)

    auth_users = response.json().get("users", [])
    roles_response = get_supabase().table("user_roles").select("user_id,role").execute()
    roles_map = {row["user_id"]: row["role"] for row in (roles_response.data or [])}

    return ok(
        [
            {
                "id": user["id"],
                "email": user.get("email", ""),
                "role": roles_map.get(user["id"], "viewer"),
                "created_at": user.get("created_at", ""),
                "last_sign_in": user.get("last_sign_in_at", ""),
            }
            for user in auth_users
        ]
    )


@require_http_methods(["PUT"])
@require_superadmin
def update_role(request, uid: str):
    try:
        payload = json.loads(request.body.decode("utf-8") or "{}")
    except json.JSONDecodeError:
        return err("Invalid JSON body", 400)

    role = payload.get("role", "")
    if role not in {"superadmin", "admin", "viewer"}:
        return err("Invalid role. Must be superadmin, admin, or viewer", 400)

    get_supabase().table("user_roles").upsert(
        {"user_id": uid, "role": role},
        on_conflict="user_id",
    ).execute()
    return ok({"ok": True, "user_id": uid, "role": role})
