import json
import os

import httpx
from django.views.decorators.http import require_GET, require_http_methods

from api.auth import get_user_role, require_auth
from api.utils import err, ok


SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY", "")
SUPABASE_ANON_KEY = os.getenv("SUPABASE_ANON_KEY", "")


@require_GET
def config(_request):
    return ok(
        {
            "supabase_url": SUPABASE_URL,
            "supabase_anon_key": SUPABASE_ANON_KEY,
        }
    )


@require_http_methods(["POST"])
def signup(request):
    try:
        body = json.loads(request.body.decode("utf-8") or "{}")
    except json.JSONDecodeError:
        return err("Invalid JSON body", 400)

    email = (body.get("email") or "").strip()
    password = body.get("password") or ""
    metadata = {
        key: value
        for key, value in {
            "full_name": body.get("full_name", ""),
            "phone": body.get("phone", ""),
        }.items()
        if value
    }

    if not email or not password:
        return err("Email and password are required.", 400)

    with httpx.Client(timeout=15.0) as client:
        create_response = client.post(
            f"{SUPABASE_URL}/auth/v1/admin/users",
            headers={
                "apikey": SUPABASE_SERVICE_KEY,
                "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "email": email,
                "password": password,
                "email_confirm": True,
                "user_metadata": metadata,
            },
        )
        if not create_response.is_success:
            data = create_response.json()
            message = (
                data.get("error_description")
                or data.get("msg")
                or data.get("message")
                or "Sign up failed."
            )
            if "already registered" in message.lower() or create_response.status_code == 422:
                message = "This email is already registered. Please sign in."
            return err(message, 400)

        signin_response = client.post(
            f"{SUPABASE_URL}/auth/v1/token?grant_type=password",
            headers={
                "apikey": SUPABASE_ANON_KEY,
                "Content-Type": "application/json",
            },
            json={"email": email, "password": password},
        )
        if not signin_response.is_success:
            return err(
                "Account created but sign-in failed. Please sign in manually.",
                400,
            )

        return ok(signin_response.json())


@require_GET
@require_auth
def me(request):
    user = request.user_data
    role = get_user_role(user["id"])
    request.user_role = role
    return ok({"id": user["id"], "email": user.get("email", ""), "role": role})
