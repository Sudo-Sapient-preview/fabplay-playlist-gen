import json
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Iterable

from brand_pipeline.day_part_templates import DAY_PART_TEMPLATES

from api.activity import log_activity
from api.db import get_supabase
from api.store import (
    delete_brand as store_delete_brand,
    get_brand,
    get_brands,
    save_brands,
)


Result = tuple[bool, str | None, int | None]
OK: Result = (True, None, None)

TARGET_KEYS = (
    "energy_target", "valence_target", "tempo_target", "danceability_target",
    "acousticness_target", "instrumentalness_target", "loudness_target", "speechiness_target",
)
PROFILE_KEYS = ("sincerity", "excitement", "competence", "sophistication", "ruggedness")

_BRAND_DEFAULTS: dict[str, Any] = {
    "brand_name": "", "category": "", "website_url": "", "brand_description": "",
    "customer_description": "", "customer_segment": "mid_range",
    "age_min": 18, "age_max": 65,
    "lifestyle_tags": [], "visitor_activity": [],
    "include_genres": [], "exclude_genres": [],
    "include_artists": [], "exclude_artists": [],
    "filter_explicit": True, "music_notes": "", "asset_analysis": "",
    "has_brand_guidelines": False,
    "include_song_types": "all",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _pick(src: dict, keys: Iterable[str], dst: dict) -> dict:
    for k in keys:
        if k in src:
            dst[k] = src[k]
    return dst


def _authorize(brand_id: str, user_id: str) -> Result:
    brand = get_brand(brand_id)
    if not brand:
        return False, "Brand not found", 404
    if brand.get("user_id") != user_id:
        return False, "Not your brand", 403
    return OK


def _mutate(brand_id: str, user_id: str, fn: Callable[[dict], Result | None]) -> Result:
    ok, msg, status = _authorize(brand_id, user_id)
    if not ok:
        return False, msg, status
    brands = get_brands()
    brand = brands.get(brand_id)
    if not brand:
        return False, "Brand not found", 404
    res = fn(brand)
    if res and not res[0]:
        return res
    brand["last_updated"] = _now()
    save_brands(brands)
    return OK


def _sync_brands_from_supabase(user_id: str) -> None:
    """Pull any brands from brand_playlists that are missing from local JSON (cross-device sync)."""
    try:
        resp = (
            get_supabase()
            .table("brand_playlists")
            .select("brand_id, playlist_json")
            .eq("user_id", user_id)
            .execute()
        )
        rows = resp.data or []
    except Exception:
        return

    if not rows:
        return

    brands = get_brands()
    changed = False
    for row in rows:
        brand_id = row.get("brand_id")
        if not brand_id or brand_id in brands:
            continue
        pj = row.get("playlist_json") or {}
        if isinstance(pj, str):
            try:
                pj = json.loads(pj)
            except Exception:
                pj = {}
        bp = pj.get("brand_profile") or {}
        brand_name = bp.get("brand_name") or brand_id
        brands[brand_id] = {
            "id": brand_id,
            "user_id": user_id,
            "brand_name": brand_name,
            "category": "",
            "status": "active",
            "playlist_count": 1,
            "playlist_name": brand_name,
            "last_updated": _now(),
            "last_generated_at": None,
            "brand_profile": bp,
            "sound_board_result": pj.get("sound_board"),
        }
        changed = True

    if changed:
        save_brands(brands)


def list_brands_for_user(user_id: str) -> list[dict]:
    _sync_brands_from_supabase(user_id)
    return [b for b in get_brands().values() if b.get("user_id") == user_id]


def create_brand(user_id: str, payload: dict) -> tuple[dict | None, str | None]:
    category = payload.get("category", "")
    if not category:
        return None, "Category is required."
    if category not in DAY_PART_TEMPLATES:
        return None, f"Invalid category '{category}'."

    brand_id = str(uuid.uuid4())
    now = _now()
    brand: dict[str, Any] = {"id": brand_id, "user_id": user_id}
    for k, default in _BRAND_DEFAULTS.items():
        brand[k] = payload.get(k, default)
    brand.update({
        "status": "setup",
        "playlist_count": 0,
        "playlist_name": payload.get("brand_name", ""),
        "last_updated": now,
        "last_generated_at": None,
        "brand_profile": None,
        "sound_board_result": None,
    })

    brands = get_brands()
    brands[brand_id] = brand
    save_brands(brands)
    log_activity("Brand created", brand["brand_name"])
    return brand, None


def delete_brand(brand_id: str, user_id: str) -> Result:
    ok, msg, status = _authorize(brand_id, user_id)
    if not ok:
        return False, msg, status
    brand = get_brand(brand_id) or {}
    name = brand.get("brand_name", "")
    store_delete_brand(brand_id)
    try:
        get_supabase().table("brand_playlists").delete().eq("brand_id", brand_id).execute()
        get_supabase().table("user_playlist_songs").delete().eq("brand_id", brand_id).execute()
    except Exception:
        pass
    log_activity("Brand deleted", name)
    return OK


def rename_playlist(brand_id: str, user_id: str, name: str) -> Result:
    clean = (name or "").strip()
    if not clean:
        return False, "Name is required", 400

    def apply(brand: dict) -> None:
        brand["playlist_name"] = clean

    return _mutate(brand_id, user_id, apply)


def update_soundboard_targets(brand_id: str, user_id: str, targets: dict) -> Result:
    if not isinstance(targets, dict):
        return False, "targets must be an object", 400

    def apply(brand: dict) -> None:
        sbr = brand.setdefault("sound_board_result", {}) or {}
        brand["sound_board_result"] = sbr
        sb = sbr.setdefault("sound_board", {}) or {}
        sbr["sound_board"] = sb
        _pick(targets, TARGET_KEYS, sb)

    return _mutate(brand_id, user_id, apply)


def update_dayparts(brand_id: str, user_id: str, day_parts: list) -> Result:
    if not isinstance(day_parts, list):
        return False, "day_parts must be a list", 400

    def apply(brand: dict) -> None:
        sbr = brand.setdefault("sound_board_result", {}) or {}
        brand["sound_board_result"] = sbr
        existing = sbr.get("day_parts") or []
        merged = []
        for i, incoming in enumerate(day_parts):
            base = dict(existing[i]) if i < len(existing) else {}
            if isinstance(incoming, dict):
                _pick(incoming, TARGET_KEYS, base)
            merged.append(base)
        if len(existing) > len(merged):
            merged.extend(existing[len(merged):])
        sbr["day_parts"] = merged

    return _mutate(brand_id, user_id, apply)


def update_profile(brand_id: str, user_id: str, payload: dict) -> Result:
    if not isinstance(payload, dict):
        return False, "payload must be an object", 400

    def apply(brand: dict) -> None:
        profile = brand.setdefault("brand_profile", {}) or {}
        brand["brand_profile"] = profile
        _pick(payload, PROFILE_KEYS, profile)

    return _mutate(brand_id, user_id, apply)


def update_genres(brand_id: str, user_id: str, include_genres, exclude_genres, include_song_types=None, include_artists=None, exclude_artists=None) -> Result:
    def apply(brand: dict) -> None:
        if include_genres is not None:
            brand["include_genres"] = list(include_genres)
        if exclude_genres is not None:
            brand["exclude_genres"] = list(exclude_genres)
        if include_song_types is not None:
            brand["include_song_types"] = include_song_types
        if include_artists is not None:
            brand["include_artists"] = list(include_artists)
        if exclude_artists is not None:
            brand["exclude_artists"] = list(exclude_artists)

    return _mutate(brand_id, user_id, apply)
