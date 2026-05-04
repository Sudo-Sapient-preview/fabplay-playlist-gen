import uuid
from datetime import datetime, timezone

from brand_pipeline.day_part_templates import DAY_PART_TEMPLATES

from api.activity import log_activity
from api.store import (
    delete_brand as store_delete_brand,
    get_brand,
    get_brands,
    get_playlists,
    save_brands,
    save_playlists,
)


def list_brands_for_user(user_id: str) -> list[dict]:
    return [brand for brand in get_brands().values() if brand.get("user_id") == user_id]


def create_brand(user_id: str, payload: dict) -> tuple[dict | None, str | None]:
    category = payload.get("category", "")
    if not category:
        return None, "Category is required."
    if category not in DAY_PART_TEMPLATES:
        return None, f"Invalid category '{category}'."

    brand_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    brand = {
        "id": brand_id,
        "user_id": user_id,
        "brand_name": payload.get("brand_name", ""),
        "category": payload.get("category", ""),
        "website_url": payload.get("website_url", ""),
        "brand_description": payload.get("brand_description", ""),
        "customer_description": payload.get("customer_description", ""),
        "customer_segment": payload.get("customer_segment", "mid_range"),
        "age_min": payload.get("age_min", 18),
        "age_max": payload.get("age_max", 65),
        "lifestyle_tags": payload.get("lifestyle_tags", []),
        "visitor_activity": payload.get("visitor_activity", []),
        "include_genres": payload.get("include_genres", []),
        "exclude_genres": payload.get("exclude_genres", []),
        "include_artists": payload.get("include_artists", []),
        "exclude_artists": payload.get("exclude_artists", []),
        "filter_explicit": payload.get("filter_explicit", True),
        "music_notes": payload.get("music_notes", ""),
        "asset_analysis": payload.get("asset_analysis", ""),
        "has_brand_guidelines": payload.get("has_brand_guidelines", False),
        "status": "setup",
        "playlist_count": 0,
        "playlist_name": payload.get("brand_name", ""),
        "last_updated": now,
        "last_generated_at": None,
        "brand_profile": None,
        "sound_board_result": None,
    }

    brands = get_brands()
    brands[brand_id] = brand
    save_brands(brands)
    log_activity("Brand created", brand["brand_name"])
    return brand, None


def delete_brand(brand_id: str, user_id: str) -> tuple[bool, str | None, int | None]:
    brand = get_brand(brand_id)
    if not brand:
        return False, "Brand not found", 404
    if brand.get("user_id") != user_id:
        return False, "Not your brand", 403

    name = brand.get("brand_name", "")
    store_delete_brand(brand_id)
    playlists = get_playlists()
    playlists.pop(brand_id, None)
    save_playlists(playlists)
    log_activity("Brand deleted", name)
    return True, None, None


def rename_playlist(brand_id: str, user_id: str, name: str) -> tuple[bool, str | None, int | None]:
    brand = get_brand(brand_id)
    if not brand:
        return False, "Brand not found", 404
    if brand.get("user_id") != user_id:
        return False, "Not your brand", 403

    clean_name = (name or "").strip()
    if not clean_name:
        return False, "Name is required", 400

    brands = get_brands()
    brands[brand_id]["playlist_name"] = clean_name
    brands[brand_id]["last_updated"] = datetime.now(timezone.utc).isoformat()
    save_brands(brands)
    return True, None, None
