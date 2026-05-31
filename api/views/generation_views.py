import json
import logging

from django.views.decorators.http import require_GET, require_http_methods

logger = logging.getLogger(__name__)

from api.auth import require_auth
from api.services.ai_service import analyze_files, quick_analyze, scrape_website, music_recs_from_content
from api.services.generation_service import (
    get_generation_task,
    start_playlist_generation,
    start_soundboard,
)
from api.store import get_brands, save_brands
from api.utils import err, ok


def _json_body(request) -> tuple[dict | None, str | None]:
    try:
        return json.loads(request.body.decode("utf-8") or "{}"), None
    except json.JSONDecodeError:
        return None, "Invalid JSON body"


def _uploaded_files(request) -> list[tuple[str, bytes]]:
    return [
        (uploaded.name or "upload", uploaded.read())
        for uploaded in request.FILES.getlist("files")
    ]


@require_http_methods(["POST"])
@require_auth
def quick_analyze_view(request):
    payload, error_message = _json_body(request)
    if error_message:
        return err(error_message, 400)

    payload = payload or {}
    website_url = payload.get("website_url", "").strip()
    brand_description = payload.get("brand_description", "").strip()

    try:
        website_content = scrape_website(website_url) if website_url else ""

        result = quick_analyze(
            payload.get("brand_name", ""),
            payload.get("category", ""),
            website_url,
            website_content,
            brand_description,
        )

        # Generate music filters from whatever content is available.
        # Priority: scraped website > brand description > nothing.
        if website_content:
            recs = music_recs_from_content(website_content)
            result["asset_analysis"] = f"[Website: {website_url}]\n{website_content[:3000]}"
        elif brand_description:
            recs = music_recs_from_content(brand_description)
            result["asset_analysis"] = f"[Brand Description]\n{brand_description[:3000]}"
        else:
            recs = {"recommended_genres": [], "avoid_genres": [], "music_notes": ""}
            result["asset_analysis"] = ""

        result["recommended_genres"] = recs.get("recommended_genres", [])
        result["avoid_genres"] = recs.get("avoid_genres", [])
        result["music_notes"] = recs.get("music_notes", "")
        return ok(result)
    except Exception as exc:
        logger.exception("quick_analyze_view failed")
        return err(str(exc), 500)


@require_http_methods(["POST"])
@require_auth
def analyze_assets_preview_view(request):
    files = _uploaded_files(request)
    if not files:
        return err("No files provided", 400)
    try:
        return ok(analyze_files(files))
    except Exception as exc:
        return err(str(exc), 500)


@require_http_methods(["POST"])
@require_auth
def upload_assets_view(request, brand_id: str):
    brands = get_brands()
    brand = brands.get(brand_id)
    if not brand:
        return err("Brand not found", 404)
    if brand.get("user_id") != request.user_data["id"]:
        return err("Not your brand", 403)

    files = _uploaded_files(request)
    if not files:
        return err("No files provided", 400)

    try:
        result = analyze_files(files)
    except Exception as exc:
        return err(str(exc), 500)

    brands = get_brands()
    if brand_id not in brands:
        return err("Brand not found", 404)
    brands[brand_id]["asset_analysis"] = result["asset_analysis"]
    brands[brand_id]["has_brand_guidelines"] = result["has_brand_guidelines"]
    brands[brand_id]["brand_profile"] = None
    brands[brand_id]["sound_board_result"] = None
    save_brands(brands)
    return ok(
        {
            "analyzed": len(files),
            "has_brand_guidelines": result["has_brand_guidelines"],
        }
    )


@require_http_methods(["POST"])
@require_auth
def start_soundboard_view(request, brand_id: str):
    task_id, message, status = start_soundboard(brand_id)
    if not task_id:
        return err(message or "Could not start soundboard generation", status or 400)
    return ok({"task_id": task_id})


@require_http_methods(["POST"])
@require_auth
def start_generate_view(request, brand_id: str):
    payload, error_message = _json_body(request)
    if error_message:
        return err(error_message, 400)

    payload = payload or {}
    overrides = payload.get("genre_overrides") or {}
    genre_overrides = {
        "include": overrides.get("include", []),
        "exclude": overrides.get("exclude", []),
    }
    playlist_name = payload.get("playlist_name")

    task_id, message, status = start_playlist_generation(
        brand_id,
        genre_overrides=genre_overrides,
        playlist_name=playlist_name,
    )
    if not task_id:
        return err(message or "Could not start playlist generation", status or 400)
    return ok({"task_id": task_id})


@require_GET
@require_auth
def generation_status_view(_request, task_id: str):
    task, message, status = get_generation_task(task_id)
    if not task:
        return err(message or "Task not found", status or 404)
    return ok(task)
