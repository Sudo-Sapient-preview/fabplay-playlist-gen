from pathlib import Path

import httpx
from django.conf import settings
from django.http import FileResponse, HttpResponse
from django.shortcuts import redirect, render


STATIC_DIR = Path(settings.BASE_DIR) / "web" / "static"


def index(_request):
    return redirect("/login")


def login_page(request):
    return render(request, "login.html")


def auth_callback(request):
    return render(request, "auth-callback.html")


def dashboard(request, path=""):
    return render(request, "index.html")


def favicon(_request):
    icon_path = STATIC_DIR / "logo.jpg"
    if not icon_path.exists():
        return HttpResponse(status=404)
    return FileResponse(icon_path.open("rb"), content_type="image/jpeg")


def songs_proxy(request, path):
    if settings.SONGS_BASE_URL:
        headers = {}
        if "HTTP_RANGE" in request.META:
            headers["Range"] = request.META["HTTP_RANGE"]
        with httpx.Client(timeout=60.0) as client:
            response = client.get(f"{settings.SONGS_BASE_URL}/{path}", headers=headers)
        proxy = HttpResponse(
            response.content,
            status=response.status_code,
            content_type=response.headers.get("content-type", "audio/mpeg"),
        )
        for header_name in ("content-range", "accept-ranges", "content-length"):
            if header_name in response.headers:
                proxy[header_name.title()] = response.headers[header_name]
        return proxy

    file_path = Path(settings.SONGS_DIR) / path
    if not file_path.exists() or not file_path.is_file():
        return HttpResponse(status=404)
    return FileResponse(file_path.open("rb"), content_type="audio/mpeg")
