"""
fabPLAY UI v3 — Lightweight frontend server
Serves the UI and proxies API calls to the MMR backend on port 8001.
"""

import httpx
from pathlib import Path
from fastapi import FastAPI, Request, UploadFile, File
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, Response, RedirectResponse

import os
from dotenv import load_dotenv
# Load .env from parent directory
load_dotenv(Path(__file__).parent.parent / ".env")

BACKEND_URL = os.getenv("BACKEND_URL", "http://localhost:8001")

app = FastAPI(title="fabPLAY UI v3")

STATIC_DIR = Path(__file__).parent / "static"
SONGS_DIR = Path(os.getenv("SONGS_DIR", str(Path(__file__).parent.parent.parent / "songs")))


@app.get("/favicon.ico")
async def favicon():
    return FileResponse(str(STATIC_DIR / "logo.jpg"), media_type="image/jpeg")


@app.get("/")
async def serve_landing():
    return RedirectResponse("/login", status_code=302)


@app.get("/login")
async def serve_login():
    return FileResponse(str(STATIC_DIR / "login.html"))


@app.get("/auth/callback")
async def serve_auth_callback():
    return FileResponse(str(STATIC_DIR / "auth-callback.html"))


@app.get("/dashboard")
async def serve_dashboard(request: Request):
    # Server-side guard: redirect to /login if no session cookie present.
    # The real security is enforced by the API's JWT check on every request.
    if not request.cookies.get("fabplay_session"):
        return RedirectResponse("/login", status_code=302)
    return FileResponse(str(STATIC_DIR / "index.html"))


@app.get("/dashboard/{path:path}")
async def serve_dashboard_paths(path: str):
    return FileResponse(str(STATIC_DIR / "index.html"))


# ── Proxy /api/* and /songs/* to backend ─────────────────────────────────────

@app.api_route("/api/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
async def proxy_api(path: str, request: Request):
    """Forward all API requests to the backend server."""
    url = f"{BACKEND_URL}/api/{path}"
    headers = {k: v for k, v in request.headers.items()
               if k.lower() not in ("host", "content-length", "transfer-encoding")}

    body = await request.body()
    async with httpx.AsyncClient(timeout=120.0) as client:
        resp = await client.request(
            method=request.method,
            url=url,
            headers=headers,
            content=body,
            params=request.query_params,
        )

    return Response(
        content=resp.content,
        status_code=resp.status_code,
        headers=dict(resp.headers),
    )


@app.api_route("/songs/{path:path}", methods=["GET"])
async def proxy_songs(path: str, request: Request):
    """Forward song file requests to the backend server (supports Range / streaming)."""
    url = f"{BACKEND_URL}/songs/{path}"
    headers = {k: v for k, v in request.headers.items()
               if k.lower() not in ("host", "content-length", "transfer-encoding")}

    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.request(
            method="GET",
            url=url,
            headers=headers,
            params=request.query_params,
        )

    return Response(
        content=resp.content,
        status_code=resp.status_code,
        headers=dict(resp.headers),
    )


# ── Static files ──────────────────────────────────────────────────────────────
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("frontend.server:app", host="0.0.0.0", port=8003, reload=True)
