import inspect
import os
import time
from functools import wraps

import httpx

from api.db import get_supabase
from api.utils import err


SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY", "")

_AUTH_CACHE_TTL = 60.0
_ROLE_CACHE_TTL = 300.0

_auth_cache: dict[str, tuple[dict, float]] = {}
_role_cache: dict[str, tuple[str, float]] = {}
_http_client: httpx.Client | None = None


def _get_http_client() -> httpx.Client:
    global _http_client
    if _http_client is None or _http_client.is_closed:
        _http_client = httpx.Client(timeout=10.0)
    return _http_client


def _extract_bearer_token(request) -> str:
    header = request.headers.get("Authorization", "").strip()
    if not header:
        return ""
    prefix, _, token = header.partition(" ")
    if prefix.lower() != "bearer":
        return ""
    return token.strip()


def authenticate_request(request) -> tuple[dict | None, str | None, int | None, str | None]:
    token = _extract_bearer_token(request)
    user_dict, status, message = validate_token(token)
    if status:
        return None, token, status, message
    request.auth_token = token
    request.user_data = user_dict
    return user_dict, token, None, None


def validate_token(token: str) -> tuple[dict | None, int | None, str | None]:
    if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
        return None, 503, "Auth service unavailable"
    if not token:
        return None, 401, "Missing bearer token"

    now = time.time()
    cached = _auth_cache.get(token)
    if cached:
        user_dict, expires_at = cached
        if now < expires_at:
            return user_dict, None, None
        _auth_cache.pop(token, None)

    try:
        response = _get_http_client().get(
            f"{SUPABASE_URL}/auth/v1/user",
            headers={
                "Authorization": f"Bearer {token}",
                "apikey": SUPABASE_SERVICE_KEY,
            },
        )
    except (httpx.ConnectTimeout, httpx.ReadTimeout, httpx.ConnectError):
        return None, 503, "Auth service unavailable"

    if response.status_code != 200:
        return None, 401, "Invalid or expired token"

    user_dict = response.json()
    _auth_cache[token] = (user_dict, now + _AUTH_CACHE_TTL)
    if len(_auth_cache) > 500:
        oldest = sorted(_auth_cache.items(), key=lambda item: item[1][1])[:100]
        for cache_key, _ in oldest:
            _auth_cache.pop(cache_key, None)
    return user_dict, None, None


def get_user_role(user_id: str) -> str:
    now = time.time()
    cached = _role_cache.get(user_id)
    if cached:
        role, expires_at = cached
        if now < expires_at:
            return role
        _role_cache.pop(user_id, None)

    result = (
        get_supabase()
        .table("user_roles")
        .select("role")
        .eq("user_id", user_id)
        .maybe_single()
        .execute()
    )
    role = result.data["role"] if (result and result.data) else "viewer"
    _role_cache[user_id] = (role, now + _ROLE_CACHE_TTL)
    return role


def require_auth(view_func):
    if inspect.iscoroutinefunction(view_func):

        @wraps(view_func)
        async def async_wrapper(request, *args, **kwargs):
            user_dict, _token, status, message = authenticate_request(request)
            if status:
                return err(message or "Unauthorized", status)
            return await view_func(request, *args, **kwargs)

        return async_wrapper

    @wraps(view_func)
    def wrapper(request, *args, **kwargs):
        user_dict, _token, status, message = authenticate_request(request)
        if status:
            return err(message or "Unauthorized", status)
        return view_func(request, *args, **kwargs)

    return wrapper


def require_superadmin(view_func):
    if inspect.iscoroutinefunction(view_func):

        @wraps(view_func)
        async def async_wrapper(request, *args, **kwargs):
            user_dict, _token, status, message = authenticate_request(request)
            if status:
                return err(message or "Unauthorized", status)
            role = get_user_role(user_dict["id"])
            if role != "superadmin":
                return err("Superadmin access required", 403)
            request.user_role = role
            return await view_func(request, *args, **kwargs)

        return async_wrapper

    @wraps(view_func)
    def wrapper(request, *args, **kwargs):
        user_dict, _token, status, message = authenticate_request(request)
        if status:
            return err(message or "Unauthorized", status)
        role = get_user_role(user_dict["id"])
        if role != "superadmin":
            return err("Superadmin access required", 403)
        request.user_role = role
        return view_func(request, *args, **kwargs)

    return wrapper
