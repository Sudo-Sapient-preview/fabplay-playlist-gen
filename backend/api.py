"""
api.py — fabPLAY v3.0 MMR Backend
Runs on port 8001. Serves all endpoints consumed by fabplay-ui-v3.

Usage:
    python api.py
"""

import os
import json
import time
import uuid
import threading
import logging
from typing import Optional
from datetime import datetime, timezone
from pathlib import Path

import httpx
import uvicorn
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, UploadFile, File, Request, Depends, APIRouter
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel
from fastapi.responses import FileResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv
from openai import AzureOpenAI

from backend.db import get_supabase, run_startup_checks, fetch_all_songs
from brand_pipeline.brand_analysis import get_brand_profile
from brand_pipeline.web_scraper import scrape_brand_website
from brand_pipeline.sound_board import get_sound_board, apply_segment_adjustments
from pipeline.rag_retriever import retrieve_candidates, fetch_must_include_tracks, fetch_must_include_genre_tracks, apply_hard_filters, apply_exclusion_filters_only
from pipeline.mmr_scorer import mmr_select, compute_relevance, compute_track_sim
from brand_pipeline.day_part_templates import DAY_PART_TEMPLATES, get_template, get_day_part_hours, target_track_count

load_dotenv()
logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

MMR_LAMBDA = float(os.getenv("MMR_LAMBDA", "0.7"))
SONGS_BASE_URL = (os.getenv("SONGS_BASE_URL", "") or "").strip().rstrip("/")

# ─── Auth config ──────────────────────────────────────────────────────────────

SUPABASE_URL         = os.getenv("SUPABASE_URL", "")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY", "")
SUPABASE_ANON_KEY    = os.getenv("SUPABASE_ANON_KEY", "")
_bearer = HTTPBearer()

# Cache validated tokens for 60 s to avoid a Supabase round-trip on every request.
_AUTH_CACHE_TTL = 60.0
_auth_cache: dict[str, tuple[dict, float]] = {}

# Cache user roles for 5 minutes — role changes are rare, DB round-trip is not worth it.
_ROLE_CACHE_TTL = 300.0
_role_cache: dict[str, tuple[str, float]] = {}

# Persistent HTTP client — reuses TCP+TLS connections instead of creating one per request.
_http_client: httpx.AsyncClient | None = None

def _get_http_client() -> httpx.AsyncClient:
    global _http_client
    if _http_client is None or _http_client.is_closed:
        _http_client = httpx.AsyncClient(timeout=10.0)
    return _http_client


async def get_current_user(
    creds: HTTPAuthorizationCredentials = Depends(_bearer),
) -> dict:
    """Validate a Supabase JWT, with a 60-second in-memory cache."""
    token = creds.credentials
    now = time.time()

    cached = _auth_cache.get(token)
    if cached:
        user_dict, expires_at = cached
        if now < expires_at:
            return user_dict
        del _auth_cache[token]

    try:
        resp = await _get_http_client().get(
            f"{SUPABASE_URL}/auth/v1/user",
            headers={
                "Authorization": f"Bearer {token}",
                "apikey": SUPABASE_SERVICE_KEY,
            },
        )
    except (httpx.ConnectTimeout, httpx.ReadTimeout, httpx.ConnectError) as e:
        raise HTTPException(status_code=503, detail="Auth service unavailable")
    if resp.status_code != 200:
        raise HTTPException(status_code=401, detail="Invalid or expired token")

    user_dict = resp.json()
    _auth_cache[token] = (user_dict, now + _AUTH_CACHE_TTL)
    # Evict entries beyond a reasonable limit to prevent unbounded growth.
    if len(_auth_cache) > 500:
        oldest = sorted(_auth_cache.items(), key=lambda x: x[1][1])[:100]
        for k, _ in oldest:
            _auth_cache.pop(k, None)
    return user_dict


async def get_current_role(user: dict = Depends(get_current_user)) -> str:
    """Fetch this user's role from the user_roles table, cached for 5 minutes."""
    user_id = user["id"]
    now = time.time()
    cached = _role_cache.get(user_id)
    if cached:
        role, expires_at = cached
        if now < expires_at:
            return role
        del _role_cache[user_id]
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


async def require_superadmin(role: str = Depends(get_current_role)):
    if role != "superadmin":
        raise HTTPException(status_code=403, detail="Superadmin access required")

@asynccontextmanager
async def lifespan(app: FastAPI):
    run_startup_checks(exit_on_fatal=False)
    _migrate_local_brands_to_supabase()
    logger.info("fabPLAY MMR API ready on http://0.0.0.0:8001")
    yield


ALLOWED_ORIGINS = os.getenv("ALLOWED_ORIGINS", "http://localhost:8003").split(",")
app = FastAPI(title="fabPLAY API v3.0 MMR", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=ALLOWED_ORIGINS, allow_methods=["*"], allow_headers=["*"])

# ─── Routers ──────────────────────────────────────────────────────────────────
public_router     = APIRouter()
protected_router  = APIRouter(dependencies=[Depends(get_current_user)])
superadmin_router = APIRouter(dependencies=[Depends(require_superadmin)])

# ─── Song file resolver ───────────────────────────────────────────────────────

SONGS_DIR = Path(os.getenv("SONGS_DIR", str(Path(__file__).parent.parent.parent / "songs")))


def _resolve_track_src(track: dict) -> str:
    """
    Resolve a playable source URL.
    - Keep absolute URLs as-is.
    - For relative DB paths, prefer SONGS_BASE_URL when configured.
    - Fall back to backend /songs proxy route.
    """
    raw = str(track.get("url") or track.get("src") or "").strip()
    if not raw:
        return ""
    if raw.startswith(("http://", "https://", "data:", "blob:")):
        return raw
    rel = raw.lstrip("/")
    if SONGS_BASE_URL:
        return f"{SONGS_BASE_URL}/{rel}"
    if rel.startswith("songs/"):
        rel = rel[len("songs/"):]
    return f"/songs/{rel}"


# ─── Persistence ──────────────────────────────────────────────────────────────

BRANDS_FILE = Path("data/brands_store.json")


def _load(path: Path, default):
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return default
    return default


def _save(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def _get_brands_db(uid: str = None) -> dict:
    """Read brands from user_playlist_songs.brand_json. Returns {brand_id: brand_json}.
    Paginates to work around Supabase's default 1000-row limit.
    Falls back to brand_name column when brand_json.brand_name is missing (SQL backfill gap)."""
    try:
        seen: dict = {}
        page_size = 1000
        offset = 0
        while True:
            query = get_supabase().table("user_playlist_songs") \
                .select("brand_id,brand_json,brand_name") \
                .not_.is_("brand_json", "null")
            if uid:
                query = query.eq("user_id", uid)
            batch = query.range(offset, offset + page_size - 1).execute().data or []
            for row in batch:
                bid = row.get("brand_id")
                if bid and bid not in seen and row.get("brand_json"):
                    bj = row["brand_json"]
                    # Patch brand_name from column if brand_json.brand_name is missing
                    col_name = row.get("brand_name") or ""
                    if not bj.get("brand_name") and col_name:
                        bj = dict(bj)
                        bj["brand_name"] = col_name
                        if not bj.get("playlist_name"):
                            bj["playlist_name"] = col_name
                    seen[bid] = bj
            if len(batch) < page_size:
                break
            offset += page_size
        # Also surface new brands stored in brand_playlists before first playlist
        try:
            bp_q = get_supabase().table("brand_playlists") \
                .select("brand_id,user_id,playlist_json") \
                .not_.is_("playlist_json", "null")
            if uid:
                bp_q = bp_q.eq("user_id", uid)
            for row in (bp_q.execute().data or []):
                bid = row.get("brand_id")
                if not bid:
                    continue
                pj = row.get("playlist_json")
                obj = (json.loads(pj) if isinstance(pj, str) else pj) if pj else {}
                bp = obj.get("brand_profile") or {}
                resolved_name = obj.get("brand_name") or bp.get("brand_name") or ""
                if bid not in seen:
                    if obj.get("__brand_config__"):
                        seen[bid] = obj
                    else:
                        seen[bid] = {
                            "id":             bid,
                            "brand_name":     resolved_name or bid,
                            "user_id":        row.get("user_id", ""),
                            "status":         "active",
                            "playlist_count": 1,
                        }
                elif not seen[bid].get("brand_name") and resolved_name:
                    seen[bid] = {**seen[bid], "brand_name": resolved_name}
        except Exception as e2:
            logger.warning("_get_brands_db bp fallback failed: %s", e2)
        return seen
    except Exception as e:
        logger.warning("_get_brands_db failed: %s", e)
        return {}


def _save_brands_db(brands: dict):
    """Update brand_json in user_playlist_songs for every song row belonging to each brand."""
    if not brands:
        return
    for bid, b in brands.items():
        uid = b.get("user_id", "")
        if not uid:
            logger.warning("_save_brands_db skipping brand %s — missing user_id", bid)
            continue
        try:
            result = get_supabase().table("user_playlist_songs") \
                .update({"brand_json": b}) \
                .eq("brand_id", bid) \
                .eq("user_id", uid) \
                .execute()
            # New brand: no playlist rows yet — store config in brand_playlists as fallback
            if not (result.data or []):
                try:
                    get_supabase().table("brand_playlists").upsert({
                        "brand_id":      bid,
                        "user_id":       uid,
                        "playlist_json": json.dumps({"__brand_config__": True, **b}),
                        "updated_at":    datetime.now(timezone.utc).isoformat(),
                    }, on_conflict="brand_id").execute()
                except Exception as upsert_err:
                    logger.warning("_save_brands_db brand_playlists upsert failed for %s: %s", bid, upsert_err)
        except Exception as e:
            logger.warning("_save_brands_db failed for brand %s: %s", bid, e)


def _get_brand_db(brand_id: str) -> Optional[dict]:
    """Fetch brand config for a single brand.
    Primary: user_playlist_songs.brand_json (brands with playlists).
    Fallback: brand_playlists (new brands stored before first playlist)."""
    try:
        r = get_supabase().table("user_playlist_songs") \
            .select("brand_json,brand_name") \
            .eq("brand_id", brand_id) \
            .not_.is_("brand_json", "null") \
            .limit(1) \
            .execute()
        rows = r.data or []
        if rows:
            bj = rows[0]["brand_json"]
            col_name = rows[0].get("brand_name") or ""
            if not bj.get("brand_name") and col_name:
                bj = dict(bj)
                bj["brand_name"] = col_name
                if not bj.get("playlist_name"):
                    bj["playlist_name"] = col_name
            return bj
        # Fallback: brand may exist in brand_playlists before any playlist is generated
        r2 = get_supabase().table("brand_playlists") \
            .select("user_id,playlist_json") \
            .eq("brand_id", brand_id) \
            .limit(1) \
            .execute()
        for row in (r2.data or []):
            pj = row.get("playlist_json")
            if pj:
                obj = json.loads(pj) if isinstance(pj, str) else pj
                if obj.get("__brand_config__"):
                    return obj
                bp = obj.get("brand_profile") or {}
                brand_name = obj.get("brand_name") or bp.get("brand_name") or brand_id
                uid = obj.get("user_id") or row.get("user_id", "")
                return {"id": brand_id, "brand_name": brand_name, "user_id": uid, "status": "active", "playlist_count": 1}
        return None
    except Exception as e:
        logger.warning("_get_brand_db failed: %s", e)
        return None


def _migrate_local_brands_to_supabase():
    """On startup, push all local brands into Supabase so the local file is no longer needed."""
    local_brands = _load(BRANDS_FILE, {})
    if not local_brands:
        return
    try:
        logger.info("Migrating %d brands from local file to Supabase...", len(local_brands))
        _save_brands_db(local_brands)
        logger.info("Brand migration complete.")
    except Exception as e:
        logger.warning("Brand migration failed: %s", e)


def get_brands(uid: str = None) -> dict:
    """Read brands from Supabase. Pass uid to filter to that user (avoids pagination issues)."""
    return _get_brands_db(uid)


def save_brands(brands: dict):
    """Write brands to Supabase only."""
    _save_brands_db(brands)


# ─── Supabase playlist helpers ────────────────────────────────────────────────

def _get_playlist_db(brand_id: str) -> Optional[dict]:
    """Read a single brand's playlist from Supabase."""
    try:
        r = get_supabase().table("brand_playlists") \
            .select("playlist_json") \
            .eq("brand_id", brand_id) \
            .maybe_single() \
            .execute()
        if r and r.data:
            raw = r.data["playlist_json"]
            return json.loads(raw) if isinstance(raw, str) else raw
    except Exception as e:
        logger.warning("_get_playlist_db failed for %s: %s", brand_id, e)
    return None


def _save_playlist_db(brand_id: str, user_id: str, playlist_json: dict):
    """Upsert a brand's playlist to Supabase."""
    get_supabase().table("brand_playlists").upsert(
        {
            "brand_id":      brand_id,
            "user_id":       user_id,
            "playlist_json": playlist_json,
            "updated_at":    datetime.now(timezone.utc).isoformat(),
        },
        on_conflict="brand_id",
    ).execute()


def _delete_playlist_db(brand_id: str):
    """Delete a brand's playlist from Supabase."""
    try:
        get_supabase().table("brand_playlists").delete().eq("brand_id", brand_id).execute()
    except Exception as e:
        logger.warning("_delete_playlist_db failed for %s: %s", brand_id, e)


def _get_all_playlists_db() -> list[dict]:
    """Fetch all playlists from Supabase (used for stats)."""
    try:
        r = get_supabase().table("brand_playlists").select("playlist_json").execute()
        result = []
        for row in (r.data or []):
            raw = row.get("playlist_json")
            if raw:
                result.append(json.loads(raw) if isinstance(raw, str) else raw)
        return result
    except Exception as e:
        logger.warning("_get_all_playlists_db failed: %s", e)
        return []


# ─── Brand helpers ────────────────────────────────────────────────────────────

def _reconcile_playlist_counts(uid: str, brands: list[dict]) -> list[dict]:
    """
    Ensures playlist_count in each brand reflects what's actually in Supabase brand_playlists.
    Corrects any brand where playlist_count=0 but a real playlist row exists.
    """
    if not brands:
        return brands
    try:
        r = get_supabase().table("brand_playlists") \
            .select("brand_id") \
            .eq("user_id", uid) \
            .execute()
        ids_with_playlist = {row["brand_id"] for row in (r.data or [])}
    except Exception as e:
        logger.warning("_reconcile_playlist_counts: supabase query failed: %s", e)
        return brands

    to_save: dict = {}
    for b in brands:
        bid = b["id"]
        if bid in ids_with_playlist and (b.get("playlist_count") or 0) == 0:
            b["playlist_count"] = 1
            to_save[bid] = dict(b)
    if to_save:
        save_brands(to_save)
    return brands


# ─── Activity log ─────────────────────────────────────────────────────────────

_activity: list[dict] = []


def log_activity(event: str, brand: str):
    _activity.insert(0, {
        "event": event, "brand": brand,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })
    del _activity[50:]


def _time_ago(ts: str) -> str:
    try:
        diff = int((datetime.now(timezone.utc) - datetime.fromisoformat(ts)).total_seconds())
        if diff < 60:    return f"{diff}s ago"
        if diff < 3600:  return f"{diff//60}m ago"
        if diff < 86400: return f"{diff//3600}h ago"
        return f"{diff//86400}d ago"
    except Exception:
        return "just now"


# ─── Task registry ────────────────────────────────────────────────────────────

_tasks: dict[str, dict] = {}


def new_task() -> str:
    tid = str(uuid.uuid4())
    _tasks[tid] = {"status": "pending", "progress": 0, "log": [], "error": None}
    return tid


def task_progress(tid: str, pct: int, msg: str = ""):
    t = _tasks.get(tid)
    if t:
        t["progress"] = pct
        if msg: t["log"].append(msg)


def task_log(tid: str, msg: str):
    t = _tasks.get(tid)
    if t: t["log"].append(msg)


def task_done(tid: str):
    t = _tasks.get(tid)
    if t:
        t["status"]   = "done"
        t["progress"] = 100


def task_error(tid: str, msg: str):
    t = _tasks.get(tid)
    if t:
        t["status"] = "error"
        t["error"]  = msg


# ─── Azure OpenAI helpers ─────────────────────────────────────────────────────

def chat_client() -> tuple[AzureOpenAI, str]:
    ep  = os.getenv("AZURE_OPENAI_ENDPOINT")
    key = os.getenv("AZURE_OPENAI_API_KEY")
    ver = os.getenv("AZURE_OPENAI_API_VERSION", "2024-06-01")
    dep = os.getenv("AZURE_OPENAI_DEPLOYMENT")
    if not (ep and key and dep):
        raise RuntimeError("AZURE_OPENAI_ENDPOINT / API_KEY / DEPLOYMENT not set")
    return AzureOpenAI(azure_endpoint=ep, api_key=key, api_version=ver), dep


def embed_client() -> tuple[AzureOpenAI, str]:
    ep  = os.getenv("AZURE_OPENAI_EMBED_ENDPOINT")
    key = os.getenv("AZURE_OPENAI_EMBED_API_KEY")
    ver = os.getenv("AZURE_OPENAI_EMBED_API_VERSION", "2024-12-01-preview")
    dep = os.getenv("AZURE_OPENAI_EMBED_DEPLOYMENT", "text-embedding-3-small")
    if not (ep and key):
        raise RuntimeError("AZURE_OPENAI_EMBED_ENDPOINT / EMBED_API_KEY not set")
    return AzureOpenAI(azure_endpoint=ep, api_key=key, api_version=ver), dep


# ─── Data formatters ──────────────────────────────────────────────────────────

def _fmt_track(t: dict) -> dict:
    return {
        "song_id":          t.get("song_id", t.get("id", "")),
        "title":            t.get("title", ""),
        "artist":           t.get("artist", ""),
        "genre":            t.get("genre", ""),
        "url":              t.get("url", ""),
        "src":              t.get("url") or t.get("src", ""),
        "bpm":              round(float(t.get("tempo_bpm", 120))),
        "bfs":              round(float(t.get("relevance_score", t.get("mmr_score", 0.5))), 3),
        "mmr_score":        round(float(t.get("mmr_score",  0.5)), 3),
        "energy":           round(float(t.get("energy",     0.5)), 3),
        "valence":          round(float(t.get("valence",    0.5)), 3),
        "danceability":     round(float(t.get("danceability",  0.5)), 3),
        "acousticness":     round(float(t.get("acousticness",  0.4)), 3),
        "instrumentalness": round(float(t.get("instrumentalness", 0.3)), 3),
        "loudness":         round(float(t.get("loudness",    -8.0)), 3),
        "speechiness":      round(float(t.get("speechness",   0.1)), 3),
        "duration_seconds": round(float(t.get("duration_seconds", 210))),
    }


def _fmt_daypart(dp: dict, playlist: list[dict]) -> dict:
    tracks  = [_fmt_track(t) for t in playlist]
    dur     = sum(t["duration_seconds"] for t in tracks)
    avg_mmr = round(sum(t["bfs"]       for t in tracks) / len(tracks), 3) if tracks else 0.0
    avg_mmr_sel = round(sum(t["mmr_score"] for t in tracks) / len(tracks), 3) if tracks else 0.0
    return {
        "name":                   dp.get("name", ""),
        "start_time":             dp.get("start_time", ""),
        "end_time":               dp.get("end_time", ""),
        "character_description":  dp.get("character_description", dp.get("character", "")),
        "genre_emphasis":         dp.get("genre_emphasis", []),
        "energy_target":          dp.get("energy_target",          0.5),
        "energy_min":             dp.get("energy_min",             0.0),
        "energy_max":             dp.get("energy_max",             1.0),
        "valence_target":         dp.get("valence_target",         0.5),
        "valence_min":            dp.get("valence_min",            0.0),
        "valence_max":            dp.get("valence_max",            1.0),
        "tempo_target":           dp.get("tempo_target",           110),
        "tempo_min":              dp.get("tempo_min",              60),
        "tempo_max":              dp.get("tempo_max",              180),
        "danceability_target":    dp.get("danceability_target",    0.5),
        "danceability_min":       dp.get("danceability_min",       0.0),
        "danceability_max":       dp.get("danceability_max",       1.0),
        "acousticness_target":    dp.get("acousticness_target",    0.4),
        "acousticness_min":       dp.get("acousticness_min",       0.0),
        "acousticness_max":       dp.get("acousticness_max",       1.0),
        "instrumentalness_target": dp.get("instrumentalness_target", 0.3),
        "instrumentalness_min":   dp.get("instrumentalness_min",   0.0),
        "instrumentalness_max":   dp.get("instrumentalness_max",   1.0),
        "loudness_target":        dp.get("loudness_target",        0.5),
        "loudness_min":           dp.get("loudness_min",           0.0),
        "loudness_max":           dp.get("loudness_max",           1.0),
        "speechiness_target":     dp.get("speechiness_target",     0.2),
        "speechiness_min":        dp.get("speechiness_min",        0.0),
        "speechiness_max":        dp.get("speechiness_max",        1.0),
        "track_count":            len(tracks),
        "total_duration_seconds": round(dur),
        "avg_bfs":                avg_mmr,
        "avg_mmr":                avg_mmr_sel,
        "tracks":                 tracks,
    }


def _pipeline_inputs(brand: dict) -> dict:
    inc_genres  = brand.get("include_genres",  [])
    exc_genres  = brand.get("exclude_genres",  [])
    inc_artists = brand.get("include_artists", [])
    exc_artists = brand.get("exclude_artists", [])

    def _as_str(v):
        if isinstance(v, list): return ", ".join(v)
        return str(v) if v else ""

    return {
        "brand_name":           brand.get("brand_name", ""),
        "business_category":    brand.get("category", ""),
        "website_url":          brand.get("website_url", ""),
        "brand_description":    brand.get("brand_description", ""),
        "customer_description": brand.get("customer_description", ""),
        "customer_segment":     brand.get("customer_segment", "mid_range"),
        "age_min":              brand.get("age_min"),
        "age_max":              brand.get("age_max"),
        "lifestyle_tags":       brand.get("lifestyle_tags", []),
        "include_genres":       _as_str(inc_genres),
        "exclude_genres":       _as_str(exc_genres),
        "include_artists":      _as_str(inc_artists),
        "exclude_artists":      _as_str(exc_artists),
        "filter_explicit":      brand.get("filter_explicit", True),
        "song_type_filter":     brand.get("song_type_filter") or None,
        "music_notes":          brand.get("music_notes", ""),
        "asset_analysis":       brand.get("asset_analysis", ""),
        "has_brand_guidelines": brand.get("has_brand_guidelines", False),
    }


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def _derive_sound_targets_from_profile(profile: dict, segment: str) -> dict:
    sincerity = float(profile.get("sincerity", 0.5))
    excitement = float(profile.get("excitement", 0.5))
    competence = float(profile.get("competence", 0.5))
    sophistication = float(profile.get("sophistication", 0.5))
    ruggedness = float(profile.get("ruggedness", 0.5))

    targets = {
        "energy_target": _clamp(
            0.35 + 0.35 * excitement - 0.15 * sophistication - 0.10 * sincerity + 0.10 * ruggedness, 0.0, 1.0
        ),
        "valence_target": _clamp(
            0.40 + 0.30 * sincerity + 0.25 * excitement - 0.10 * ruggedness, 0.0, 1.0
        ),
        "tempo_target": int(round(_clamp(
            90 + 45 * excitement + 10 * ruggedness - 15 * sophistication, 60, 200
        ))),
        "danceability_target": _clamp(
            0.35 + 0.40 * excitement - 0.10 * sincerity, 0.0, 1.0
        ),
        "acousticness_target": _clamp(
            0.25 + 0.45 * sincerity + 0.20 * sophistication - 0.20 * excitement, 0.0, 1.0
        ),
        "instrumentalness_target": _clamp(
            0.20 + 0.45 * sophistication + 0.15 * competence - 0.20 * ruggedness, 0.0, 1.0
        ),
        "loudness_target": _clamp(
            0.45 + 0.30 * excitement + 0.25 * ruggedness - 0.20 * sophistication, 0.0, 1.0
        ),
        "speechiness_target": _clamp(
            0.20 + 0.20 * ruggedness - 0.15 * sophistication, 0.0, 1.0
        ),
    }

    # Keep customer-segment baseline behavior consistent with sound_board.py.
    seg_adj = {
        "value":     {"energy": +0.10, "tempo": +5,  "acousticness": -0.05},
        "mid_range": {"energy":  0.00, "tempo":  0,  "acousticness":  0.00},
        "premium":   {"energy": -0.05, "tempo": -5,  "acousticness": +0.05},
        "luxury":    {"energy": -0.10, "tempo": -10, "acousticness": +0.10},
    }.get(segment, {"energy": 0.0, "tempo": 0, "acousticness": 0.0})

    targets["energy_target"] = _clamp(targets["energy_target"] + seg_adj["energy"], 0.0, 1.0)
    targets["tempo_target"] = int(round(_clamp(targets["tempo_target"] + seg_adj["tempo"], 60, 200)))
    targets["acousticness_target"] = _clamp(
        targets["acousticness_target"] + seg_adj["acousticness"], 0.0, 1.0
    )
    return targets


def _apply_profile_targets_to_soundboard(brand: dict) -> bool:
    sb_result = brand.get("sound_board_result")
    bp = brand.get("brand_profile")
    if not sb_result or not isinstance(sb_result, dict) or not bp:
        return False

    sb = sb_result.setdefault("sound_board", {})
    segment = brand.get("customer_segment", "mid_range")
    new_targets = _derive_sound_targets_from_profile(bp, segment)

    old_targets = {
        k: sb.get(k)
        for k in new_targets.keys()
    }
    for key, val in new_targets.items():
        sb[key] = val

    # Move each day-part target by the same delta so profile edits ripple through
    # while preserving each day-part's relative contour.
    for dp in sb_result.get("day_parts", []):
        for key, new_val in new_targets.items():
            old_global = old_targets.get(key)
            cur = dp.get(key)
            if old_global is None or cur is None:
                dp[key] = new_val
            else:
                delta = float(cur) - float(old_global)
                if key == "tempo_target":
                    dp[key] = int(round(_clamp(new_val + delta, 60, 200)))
                else:
                    dp[key] = round(_clamp(float(new_val) + delta, 0.0, 1.0), 3)

        for base in ("energy", "valence", "tempo"):
            t_key = f"{base}_target"
            min_key = f"{base}_min"
            max_key = f"{base}_max"
            if min_key not in dp or max_key not in dp or t_key not in dp:
                continue
            t = float(dp[t_key])
            old_min = float(dp[min_key])
            old_max = float(dp[max_key])
            if old_min > old_max:
                old_min, old_max = old_max, old_min
            half = (old_max - old_min) / 2.0
            if base == "tempo":
                dp[min_key] = int(round(_clamp(t - half, 60, 200)))
                dp[max_key] = int(round(_clamp(t + half, 60, 200)))
                if dp[min_key] > dp[max_key]:
                    dp[min_key], dp[max_key] = dp[max_key], dp[min_key]
                dp[t_key] = int(round(_clamp(float(dp[t_key]), dp[min_key], dp[max_key])))
            else:
                dp[min_key] = round(_clamp(t - half, 0.0, 1.0), 3)
                dp[max_key] = round(_clamp(t + half, 0.0, 1.0), 3)
                if dp[min_key] > dp[max_key]:
                    dp[min_key], dp[max_key] = dp[max_key], dp[min_key]
                dp[t_key] = round(_clamp(float(dp[t_key]), dp[min_key], dp[max_key]), 3)
    return True


# ─── Background: Sound Board ──────────────────────────────────────────────────

def _bg_soundboard(brand_id: str, tid: str):
    try:
        brand = _get_brand_db(brand_id)
        if not brand:
            task_error(tid, f"Brand {brand_id} not found"); return

        task_progress(tid, 10, "Analysing brand identity...")
        cc, dep = chat_client()
        inputs  = _pipeline_inputs(brand)
        if inputs.get("website_url"):
            inputs["scraped_content"] = scrape_brand_website(inputs["website_url"], 3000)

        bp = get_brand_profile(inputs, cc, dep)
        bp.setdefault("brand_name",           brand["brand_name"])
        bp.setdefault("customer_description", brand.get("customer_description", ""))
        bp.setdefault("price_positioning",    brand.get("customer_segment", "mid_range"))

        task_progress(tid, 55, "Generating sound board parameters...")
        raw_cat = brand.get("category") or "cafe"
        cat = raw_cat if raw_cat in DAY_PART_TEMPLATES else "cafe"
        sb = get_sound_board(bp, cat, cc, dep, music_notes=brand.get("music_notes", ""))
        sb = apply_segment_adjustments(sb, brand.get("customer_segment", "mid_range"))

        brand.update({
            "brand_profile":      bp,
            "sound_board_result": sb,
            "status":             "active",
            "last_updated":       datetime.now(timezone.utc).isoformat(),
        })
        save_brands({brand_id: brand})
        log_activity("Sound board generated", brand["brand_name"])
        task_progress(tid, 100, "Sound board ready.")
        task_done(tid)

    except Exception as e:
        logger.exception("Sound board failed")
        task_error(tid, str(e))


# ─── Background: Playlist generation (MMR) ───────────────────────────────────

def _bg_playlist(brand_id: str, tid: str, genre_overrides: Optional[dict] = None, playlist_name: Optional[str] = None, song_type_filter: Optional[str] = None, artist_overrides: Optional[dict] = None):
    try:
        brand = _get_brand_db(brand_id)
        if not brand:
            task_error(tid, f"Brand {brand_id} not found"); return

        bp        = brand.get("brand_profile")
        sb_result = brand.get("sound_board_result")

        def _apply_genre_overrides(inputs: dict) -> dict:
            if not genre_overrides:
                return inputs
            ov_inc = genre_overrides.get("include", [])
            ov_exc = genre_overrides.get("exclude", [])
            stored_inc = [g.strip() for g in (inputs.get("include_genres") or "").split(",") if g.strip()]
            stored_exc = [g.strip() for g in (inputs.get("exclude_genres") or "").split(",") if g.strip()]
            merged_inc = [g for g in dict.fromkeys(stored_inc + ov_inc) if g not in ov_exc]
            merged_exc = list(dict.fromkeys(stored_exc + ov_exc))
            inputs["include_genres"] = ", ".join(merged_inc)
            inputs["exclude_genres"] = ", ".join(merged_exc)
            return inputs

        def _apply_artist_overrides(inputs: dict) -> dict:
            if not artist_overrides:
                return inputs
            ov_inc = artist_overrides.get("include", [])
            ov_exc = artist_overrides.get("exclude", [])
            stored_inc = [a.strip() for a in (inputs.get("include_artists") or "").split(",") if a.strip()]
            stored_exc = [a.strip() for a in (inputs.get("exclude_artists") or "").split(",") if a.strip()]
            merged_inc = [a for a in dict.fromkeys(stored_inc + ov_inc) if a not in ov_exc]
            merged_exc = list(dict.fromkeys(stored_exc + ov_exc))
            inputs["include_artists"] = ", ".join(merged_inc)
            inputs["exclude_artists"] = ", ".join(merged_exc)
            return inputs

        if not bp or not sb_result:
            task_progress(tid, 5, "Generating brand profile...")
            cc, dep = chat_client()
            inputs  = _apply_genre_overrides(_pipeline_inputs(brand))
            if inputs.get("website_url"):
                inputs["scraped_content"] = scrape_brand_website(inputs["website_url"], 3000)
            bp = get_brand_profile(inputs, cc, dep)
            bp.setdefault("brand_name",           brand["brand_name"])
            bp.setdefault("customer_description", brand.get("customer_description", ""))
            bp.setdefault("price_positioning",    brand.get("customer_segment", "mid_range"))

            task_progress(tid, 20, "Generating sound board...")
            raw_cat = brand.get("category") or "cafe"
            cat = raw_cat if raw_cat in DAY_PART_TEMPLATES else "cafe"
            sb_result = get_sound_board(bp, cat, cc, dep, music_notes=brand.get("music_notes", ""))
            sb_result = apply_segment_adjustments(sb_result, brand.get("customer_segment", "mid_range"))

            brand["brand_profile"]      = bp
            brand["sound_board_result"] = sb_result
            save_brands({brand_id: brand})

        task_progress(tid, 30, "Starting MMR selection...")
        inputs    = _apply_artist_overrides(_apply_genre_overrides(_pipeline_inputs(brand)))
        # Request-level song_type_filter overrides the brand-level default
        if song_type_filter:
            inputs["song_type_filter"] = song_type_filter.strip().lower()
        day_parts = sb_result.get("day_parts", [])

        include_list       = [a.strip() for a in (inputs.get("include_artists") or "").split(",") if a.strip()]
        include_genre_list = [g.strip() for g in (genre_overrides or {}).get("include", []) if g.strip()]
        logger.warning("genre injection list: %s", include_genre_list)
        if include_genre_list:
            task_log(tid, f"  Genre overrides (include): {', '.join(include_genre_list)}")

        assembled = []
        n = max(len(day_parts), 1)
        used_song_ids: set = set()   # cross-day-part dedup: tracks used in earlier parts

        for idx, dp in enumerate(day_parts):
            dp_name = dp.get("name", f"Day-Part {idx+1}")
            pct = 30 + int((idx / n) * 65)
            task_progress(tid, pct, f"Running MMR for {dp_name}...")

            candidates, all_playable, stats = retrieve_candidates(
                brand_profile   = bp,
                day_part_params = dp,
                inputs          = inputs,
                used_song_ids   = used_song_ids,
            )

            if not candidates:
                task_log(tid, f"  {dp_name}: skipped (no candidates found)")
                assembled.append(_fmt_daypart(dp, []))
                continue
            if stats.get("skipped"):
                task_log(tid, f"  {dp_name}: few candidates ({len(candidates)}), proceeding...")

            # ── Cross-day-part dedup ───────────────────────────────────────────
            # Remove songs already used in a previous day-part.
            # Fall back to the full candidate list only when dedup leaves too few.
            MIN_FRESH = 5
            fresh_candidates = [c for c in candidates if c.get("song_id") not in used_song_ids]
            if len(fresh_candidates) >= MIN_FRESH:
                candidates = fresh_candidates
                task_log(tid, f"  {dp_name}: {len(candidates)} fresh candidates after cross-part dedup")
            else:
                task_log(tid, f"  {dp_name}: catalog too small for full dedup, reusing pool")

            target_n   = target_track_count(dp)
            must_inc   = fetch_must_include_tracks(include_list, candidates, target_n)
            genre_inc  = fetch_must_include_genre_tracks(include_genre_list, candidates, target_n)

            logger.warning("genre_inc for %s: %s", dp_name, [(t.get('title'), t.get('genre')) for t in genre_inc])
            if genre_inc:
                task_log(tid, f"  {dp_name}: injecting {len(genre_inc)} genre track(s): {', '.join(t.get('title','?') for t in genre_inc)}")
            elif include_genre_list:
                task_log(tid, f"  {dp_name}: no tracks found in DB for genres: {', '.join(include_genre_list)}")

            genre_ids = {t.get("song_id") for t in candidates}
            for t in genre_inc:
                if t.get("song_id") not in genre_ids:
                    candidates.append(t)
                    genre_ids.add(t.get("song_id"))
            must_inc = list({t.get("song_id"): t for t in must_inc + genre_inc}.values())

            hours          = get_day_part_hours(dp)
            target_seconds = hours * 3600

            # Spillover: songs ranked beyond MAX_CANDIDATES — already exclusion-filtered.
            # Used to fill duration if the top candidates are exhausted.
            candidate_ids = {t.get("song_id") for t in candidates}
            spillover = [
                t for t in all_playable
                if t.get("song_id") not in candidate_ids
                and t.get("song_id") not in used_song_ids
            ]

            playlist = mmr_select(
                candidates     = candidates,
                must_include   = must_inc,
                dp             = dp,
                target_count   = target_n,
                target_seconds = target_seconds,
                lam            = MMR_LAMBDA,
                spillover      = spillover,
            )

            # Record which songs were used so later day-parts skip them
            for t in playlist:
                sid = t.get("song_id")
                if sid:
                    used_song_ids.add(sid)

            if playlist:
                task_log(tid, f"  {dp_name}: {len(playlist)} tracks selected (MMR λ={MMR_LAMBDA})")
            else:
                task_log(tid, f"  {dp_name}: 0 tracks")

            assembled.append(_fmt_daypart(dp, playlist))

        sb = sb_result.get("sound_board", {})
        playlist_result = {
            "brand_profile": bp,
            "sound_board": {
                "brand_summary":           bp.get("brand_summary", ""),
                "primary_genres":          sb.get("primary_genres",  []),
                "secondary_genres":        sb.get("secondary_genres", []),
                "energy_target":           sb.get("energy_target",          0.5),
                "valence_target":          sb.get("valence_target",         0.5),
                "tempo_target":            sb.get("tempo_target",           110),
                "danceability_target":     sb.get("danceability_target",    0.5),
                "acousticness_target":     sb.get("acousticness_target",    0.4),
                "instrumentalness_target": sb.get("instrumentalness_target", 0.3),
                "loudness_target":         0.5,
                "speechiness_target":      0.2,
            },
            "day_parts": assembled,
        }

        user_id = brand.get("user_id")
        now_iso = datetime.now(timezone.utc).isoformat()

        # ── Save playlist to Supabase ─────────────────────────────────────────
        _save_playlist_db(brand_id, user_id, playlist_result)
        logger.info("Playlist saved to brand_playlists (brand=%s)", brand_id)

        # ── Sync flat song list to user_playlist_songs ─────────────────────────
        if user_id:
            unique_songs: dict[str, str] = {}
            for dp_result in assembled:
                for track in dp_result.get("tracks", []):
                    sid = track.get("song_id")
                    if sid and sid not in unique_songs:
                        unique_songs[sid] = track.get("title", "")
            if unique_songs:
                try:
                    get_supabase().table("user_playlist_songs") \
                        .delete().eq("user_id", user_id).eq("brand_id", brand_id).execute()
                    rows = [
                        {"user_id": user_id, "brand_id": brand_id,
                         "brand_name": brand.get("brand_name", ""),
                         "song_id": sid, "song_name": name, "generated_at": now_iso,
                         "brand_json": brand}
                        for sid, name in unique_songs.items()
                    ]
                    get_supabase().table("user_playlist_songs").insert(rows).execute()
                    logger.info("Synced %d songs to user_playlist_songs (user=%s brand=%s)",
                                len(rows), user_id, brand_id)
                except Exception as e:
                    logger.warning("Could not sync playlist songs to Supabase: %s", e)

        is_first = brand.get("playlist_count", 0) == 0
        now_iso = datetime.now(timezone.utc).isoformat()
        brand["playlist_count"]    = brand.get("playlist_count", 0) + 1
        brand["last_updated"]      = now_iso
        brand["last_generated_at"] = now_iso
        brand["status"]            = "active"
        if playlist_name and is_first:
            brand["playlist_name"] = playlist_name.strip()
        elif not brand.get("playlist_name"):
            brand["playlist_name"] = brand.get("brand_name", "")
        save_brands({brand_id: brand})

        log_activity("Playlists generated (MMR)", brand["brand_name"])
        task_progress(tid, 100, "All playlists ready.")
        task_done(tid)

    except Exception as e:
        logger.exception("Playlist generation failed")
        task_error(tid, str(e))


# ─── Routes: Debug ────────────────────────────────────────────────────────────

@superadmin_router.get("/api/debug/search")
def debug_search():
    try:
        zero_vec = "[" + ",".join(["0.0"] * 1536) + "]"
        result = get_supabase().rpc(
            "search_songs_by_embedding",
            {"query_embedding": zero_vec, "match_count": 5, "min_similarity": 0.0},
        ).execute()
        error = getattr(result, "error", None)
        rows  = result.data or []
        return {
            "rpc_exists": True,
            "error":      str(error) if error else None,
            "rows_returned": len(rows),
            "sample": rows[:2],
            "song_embeddings_count": get_supabase().table("song_embeddings").select("id", count="exact").execute().count,
        }
    except Exception as e:
        return {"rpc_exists": False, "error": str(e)}


# ─── Routes: Dashboard ────────────────────────────────────────────────────────

@protected_router.get("/api/stats")
def stats(user: dict = Depends(get_current_user)):
    brands   = get_brands(user["id"])
    active   = sum(1 for b in brands.values() if b.get("status") == "active")
    all_pls  = _get_all_playlists_db()
    mmr_vals = [
        dp["avg_bfs"]
        for pl in all_pls
        for dp in pl.get("day_parts", [])
        if dp.get("avg_bfs", 0) > 0
    ]
    return {
        "total_brands":         len(brands),
        "active_sound_boards":  active,
        "playlists_this_month": len(all_pls),
        "avg_bfs":              round(sum(mmr_vals) / len(mmr_vals), 3) if mmr_vals else 0.0,
    }


@protected_router.get("/api/activity")
def activity():
    return [{"time_ago": _time_ago(a["timestamp"]), **a} for a in _activity[:10]]


# ─── Routes: Brands ───────────────────────────────────────────────────────────

@protected_router.get("/api/brands")
def list_brands(user: dict = Depends(get_current_user)):
    uid = user["id"]
    brands = list(get_brands(uid).values())
    return _reconcile_playlist_counts(uid, brands)


@protected_router.post("/api/brands")
async def create_brand(req: Request, user: dict = Depends(get_current_user)):
    data     = await req.json()
    category = data.get("category", "")
    if not category:
        raise HTTPException(400, "Category is required.")
    if category not in DAY_PART_TEMPLATES:
        raise HTTPException(400, f"Invalid category '{category}'.")
    brand_id = str(uuid.uuid4())
    now      = datetime.now(timezone.utc).isoformat()
    brand = {
        "id":                   brand_id,
        "user_id":              user["id"],
        "brand_name":           data.get("brand_name", ""),
        "category":             data.get("category", ""),
        "website_url":          data.get("website_url", ""),
        "brand_description":    data.get("brand_description", ""),
        "customer_description": data.get("customer_description", ""),
        "customer_segment":     data.get("customer_segment", "mid_range"),
        "age_min":              data.get("age_min", 18),
        "age_max":              data.get("age_max", 65),
        "lifestyle_tags":       data.get("lifestyle_tags", []),
        "visitor_activity":     data.get("visitor_activity", []),
        "include_genres":       data.get("include_genres", []),
        "exclude_genres":       data.get("exclude_genres", []),
        "include_artists":      data.get("include_artists", []),
        "exclude_artists":      data.get("exclude_artists", []),
        "filter_explicit":      data.get("filter_explicit", True),
        "song_type_filter":     data.get("song_type_filter") or None,
        "music_notes":          data.get("music_notes", ""),
        "asset_analysis":       data.get("asset_analysis", ""),
        "has_brand_guidelines": data.get("has_brand_guidelines", False),
        "status":               "setup",
        "playlist_count":       0,
        "playlist_name":        data.get("brand_name", ""),
        "last_updated":         now,
        "last_generated_at":    None,
        "brand_profile":        None,
        "sound_board_result":   None,
    }
    save_brands({brand_id: brand})
    log_activity("Brand created", brand["brand_name"])
    return brand


@protected_router.delete("/api/brands/{brand_id}")
def delete_brand(brand_id: str, user: dict = Depends(get_current_user)):
    brand = _get_brand_db(brand_id)
    if not brand:
        raise HTTPException(404, "Brand not found")
    if brand.get("user_id") != user["id"]:
        raise HTTPException(403, "Not your brand")
    name = brand.get("brand_name", "")
    _delete_playlist_db(brand_id)
    try:
        get_supabase().table("user_playlist_songs").delete().eq("brand_id", brand_id).execute()
    except Exception as e:
        logger.warning("Failed to delete user_playlist_songs for brand %s: %s", brand_id, e)
    log_activity("Brand deleted", name)
    return {"ok": True}


@protected_router.patch("/api/brands/{brand_id}/playlist-name")
async def rename_playlist(brand_id: str, req: Request, user: dict = Depends(get_current_user)):
    brand = _get_brand_db(brand_id)
    if not brand:
        raise HTTPException(404, "Brand not found")
    if brand.get("user_id") != user["id"]:
        raise HTTPException(403, "Not your brand")
    data = await req.json()
    name = (data.get("name") or "").strip()
    if not name:
        raise HTTPException(400, "Name is required")
    brand["playlist_name"] = name
    brand["last_updated"]  = datetime.now(timezone.utc).isoformat()
    save_brands({brand_id: brand})
    return {"ok": True}


@protected_router.post("/api/analyze-assets-preview")
async def analyze_assets_preview(files: list[UploadFile] = File(...)):
    import base64, fitz

    cc, dep = chat_client()
    analyses = []

    for f in files:
        content = await f.read()
        fname_lower = (f.filename or "").lower()
        try:
            if fname_lower.endswith(".pdf"):
                doc = fitz.open(stream=content, filetype="pdf")
                text = "\n".join(page.get_text() for page in doc)
                doc.close()
                resp = cc.chat.completions.create(
                    model=dep,
                    max_completion_tokens=600,
                    messages=[
                        {"role": "system", "content": (
                            "You are a brand strategist. Extract all brand identity signals from this document: "
                            "visual style, tone of voice, personality, target customer, aesthetic values, "
                            "atmosphere/music cues, colour palette, and any explicit brand rules. Be specific."
                        )},
                        {"role": "user", "content": f"File: {f.filename}\n\n{text[:8000]}"},
                    ],
                )
                analyses.append(f"[{f.filename}]:\n{resp.choices[0].message.content}")

            elif any(fname_lower.endswith(ext) for ext in (".jpg", ".jpeg", ".png", ".webp", ".gif")):
                mime = (
                    "image/jpeg" if fname_lower.endswith((".jpg", ".jpeg")) else
                    "image/png"  if fname_lower.endswith(".png")  else
                    "image/webp" if fname_lower.endswith(".webp") else
                    "image/gif"
                )
                b64 = base64.b64encode(content).decode()
                resp = cc.chat.completions.create(
                    model=dep,
                    max_completion_tokens=400,
                    messages=[
                        {"role": "system", "content": (
                            "You are a brand strategist. Analyse this brand asset image and extract brand identity signals: "
                            "visual style, colour palette, mood, personality, aesthetic, and music/atmosphere cues it suggests."
                        )},
                        {"role": "user", "content": [
                            {"type": "text", "text": f"Brand asset: {f.filename}"},
                            {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
                        ]},
                    ],
                )
                analyses.append(f"[{f.filename}]:\n{resp.choices[0].message.content}")

        except Exception as e:
            logger.warning("Asset preview analysis failed for %s: %s", f.filename, e)
            analyses.append(f"[{f.filename}]: (analysis failed — {e})")

    asset_analysis = "\n\n".join(analyses)
    music_recs: dict = {"recommended_genres": [], "avoid_genres": [], "music_notes": ""}

    if asset_analysis:
        try:
            from backend.db import fetch_all_songs as _fetch_songs
            _db_genres = sorted({s.get("genre") for s in _fetch_songs() if s.get("genre")})
            _genre_list = ", ".join(
                {"hip_hop": "Hip Hop", "soul_funk": "Soul/Funk"}.get(g, g.replace("_", " ").title())
                for g in _db_genres
            )
            MUSIC_EXTRACT_PROMPT = f"""\
You are a brand strategist. Based on this brand asset analysis, return ONLY a valid JSON object:
{{
  "recommended_genres": ["genre1", "genre2", "genre3"],
  "avoid_genres": ["genre1", "genre2"],
  "music_notes": "Brief note about music direction (1-2 sentences)"
}}
Only include genres from this list: {_genre_list}"""
            resp2 = cc.chat.completions.create(
                model=dep,
                max_completion_tokens=300,
                response_format={"type": "json_object"},
                messages=[
                    {"role": "system", "content": MUSIC_EXTRACT_PROMPT},
                    {"role": "user",   "content": asset_analysis},
                ],
            )
            music_recs = json.loads(resp2.choices[0].message.content)
        except Exception as e:
            logger.warning("Music recs extraction failed: %s", e)

    return {
        "asset_analysis":      asset_analysis,
        "recommended_genres":  music_recs.get("recommended_genres", []),
        "avoid_genres":        music_recs.get("avoid_genres", []),
        "music_notes":         music_recs.get("music_notes", ""),
    }


@protected_router.post("/api/brands/{brand_id}/assets")
async def upload_assets(brand_id: str, files: list[UploadFile] = File(...), user: dict = Depends(get_current_user)):
    import base64, fitz

    brand = _get_brand_db(brand_id)
    if not brand:
        raise HTTPException(404, "Brand not found")
    if brand.get("user_id") != user["id"]:
        raise HTTPException(403, "Not your brand")

    cc, dep = chat_client()
    analyses = []
    has_brand_guidelines = False
    GUIDELINE_KEYWORDS = {"guideline", "guide", "brand", "style", "identity", "manual", "standard"}

    for f in files:
        content = await f.read()
        fname_lower = (f.filename or "").lower()
        if any(kw in fname_lower for kw in GUIDELINE_KEYWORDS):
            has_brand_guidelines = True
        try:
            if fname_lower.endswith(".pdf"):
                doc = fitz.open(stream=content, filetype="pdf")
                text = "\n".join(page.get_text() for page in doc)
                doc.close()
                resp = cc.chat.completions.create(
                    model=dep,
                    max_completion_tokens=600,
                    messages=[
                        {"role": "system", "content": (
                            "You are a brand strategist. Extract all brand identity signals from this document: "
                            "visual style, tone of voice, personality, target customer, aesthetic values, "
                            "atmosphere/music cues, colour palette, and any explicit brand rules. Be specific."
                        )},
                        {"role": "user", "content": f"File: {f.filename}\n\n{text[:8000]}"},
                    ],
                )
                analyses.append(f"[{f.filename}]:\n{resp.choices[0].message.content}")

            elif any(fname_lower.endswith(ext) for ext in (".jpg", ".jpeg", ".png", ".webp", ".gif")):
                mime = (
                    "image/jpeg" if fname_lower.endswith((".jpg", ".jpeg")) else
                    "image/png"  if fname_lower.endswith(".png")  else
                    "image/webp" if fname_lower.endswith(".webp") else
                    "image/gif"
                )
                b64 = base64.b64encode(content).decode()
                resp = cc.chat.completions.create(
                    model=dep,
                    max_completion_tokens=400,
                    messages=[
                        {"role": "system", "content": (
                            "You are a brand strategist. Analyse this brand asset image and extract brand identity signals: "
                            "visual style, colour palette, mood, personality, aesthetic, and music/atmosphere cues it suggests."
                        )},
                        {"role": "user", "content": [
                            {"type": "text", "text": f"Brand asset: {f.filename}"},
                            {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
                        ]},
                    ],
                )
                analyses.append(f"[{f.filename}]:\n{resp.choices[0].message.content}")

        except Exception as e:
            logger.warning("Asset analysis failed for %s: %s", f.filename, e)
            analyses.append(f"[{f.filename}]: (analysis failed — {e})")

    if analyses:
        brand["asset_analysis"]       = "\n\n".join(analyses)
        brand["has_brand_guidelines"] = has_brand_guidelines
        brand["brand_profile"]        = None
        brand["sound_board_result"]   = None
        save_brands({brand_id: brand})

    return {"analyzed": len(analyses), "has_brand_guidelines": has_brand_guidelines}


# ─── Routes: Catalog ──────────────────────────────────────────────────────────

@protected_router.get("/api/catalog/genres")
def catalog_genres():
    """Return distinct genres present in the songs catalog, sorted alphabetically."""
    try:
        from backend.db import fetch_all_songs as _fetch
        songs = _fetch()
        genres = sorted({s.get("genre") for s in songs if s.get("genre")})
        return {"genres": genres}
    except Exception as e:
        return {"genres": [], "error": str(e)}


@protected_router.get("/api/catalog/artists")
def catalog_artists():
    """Return distinct non-empty artists present in the songs catalog, sorted alphabetically."""
    try:
        from backend.db import fetch_all_songs as _fetch
        songs = _fetch()
        artists = sorted({s.get("artist") for s in songs if s.get("artist")})
        return {"artists": artists}
    except Exception as e:
        return {"artists": [], "error": str(e)}


@protected_router.get("/api/catalog/stats")
def catalog_stats():
    try:
        r = get_supabase().table("songs").select("id", count="exact").execute()
        return {"total_songs": r.count or 0, "db": "Supabase", "status": "live"}
    except Exception as e:
        return {"total_songs": 0, "db": "Supabase", "status": "error", "error": str(e)}


@protected_router.get("/api/catalog/songs")
def catalog_songs():
    try:
        r = get_supabase().table("songs").select(
            "id,title,artist,genre,url,tempo_bpm,energy,valence,"
            "danceability,acousticness,instrumentalness,loudness,speechness,duration_seconds"
        ).limit(50).execute()
        return {"songs": [
            {
                "id":               s.get("id"),
                "title":            s.get("title", ""),
                "artist":           s.get("artist", ""),
                "genre":            s.get("genre", ""),
                "url":              s.get("url", ""),
                "tempo_bpm":        s.get("tempo_bpm", 120),
                "energy":           s.get("energy", 0.5),
                "valence":          s.get("valence", 0.5),
                "danceability":     s.get("danceability", 0.5),
                "acousticness":     s.get("acousticness", 0.4),
                "instrumentalness": s.get("instrumentalness", 0.3),
                "loudness":         s.get("loudness", -8.0),
                "speechiness":      s.get("speechness", 0.1),
                "duration_seconds": s.get("duration_seconds", 210),
            }
            for s in (r.data or [])
        ]}
    except Exception as e:
        return {"songs": [], "error": str(e)}


# ─── Routes: Quick Analyze ────────────────────────────────────────────────────

QUICK_ANALYZE_PROMPT = """\
You are a brand strategist. Given a brand name, business category, optional website URL, and optional scraped website content, return ONLY a valid JSON object:
{
  "customer_segment": "budget"|"mid_range"|"premium"|"luxury",
  "age_min": int,
  "age_max": int,
  "brand_description": "2-3 sentence brand narrative",
  "suggested_activities": ["activity1", "activity2", "activity3", "activity4", "activity5"],
  "suggested_customer_types": ["type1", "type2", "type3", "type4", "type5"],
  "suggested_lifestyle": ["tag1", "tag2", "tag3", "tag4", "tag5"]
}

Rules:
- customer_segment, brand_description: infer from brand name, website content (if provided), and category.
- suggested_activities: 4-6 things customers typically do at/with this brand. If website content is provided, make these SPECIFIC to this brand's actual context (e.g. for a wedding wear brand: "Shopping for wedding outfits", "Attending a fitting session", "Gifting occasion wear"). Otherwise fall back to category-level examples.
- suggested_customer_types: 4-6 types of people who visit this brand. If website content is provided, make these SPECIFIC to the actual customer base. Otherwise use category defaults.
- suggested_lifestyle: 4-6 lifestyle descriptors. If website content is provided, tailor to this brand's identity. Otherwise use category defaults.

When website content is available, prefer brand-specific suggestions over generic category defaults."""


@protected_router.post("/api/quick-analyze")
async def quick_analyze(req: Request):
    import asyncio
    data = await req.json()
    brand_name  = data.get("brand_name", "")
    website_url = data.get("website_url", "")
    category    = data.get("category", "")

    scraped = await asyncio.to_thread(scrape_brand_website, website_url, 2000)

    user_msg = f"Brand: {brand_name}\nCategory: {category or 'not provided'}\nWebsite: {website_url or 'not provided'}"
    if scraped:
        user_msg += f"\n\n=== SCRAPED WEBSITE CONTENT ===\n{scraped}\n=== END ==="

    try:
        cc, dep = chat_client()
        resp = cc.chat.completions.create(
            model=dep,
            max_completion_tokens=600,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": QUICK_ANALYZE_PROMPT},
                {"role": "user",   "content": user_msg},
            ],
        )
        return json.loads(resp.choices[0].message.content)
    except Exception as e:
        raise HTTPException(500, str(e))


# ─── Routes: Generation ───────────────────────────────────────────────────────

@protected_router.post("/api/soundboard/{brand_id}")
def start_soundboard(brand_id: str, user: dict = Depends(get_current_user)):
    brand = _get_brand_db(brand_id)
    if not brand or brand.get("user_id") != user["id"]:
        raise HTTPException(404, "Brand not found")
    tid = new_task()
    threading.Thread(target=_bg_soundboard, args=(brand_id, tid), daemon=True).start()
    return {"task_id": tid}


class GenreOverrides(BaseModel):
    include: list[str] = []
    exclude: list[str] = []

class ArtistOverrides(BaseModel):
    include: list[str] = []
    exclude: list[str] = []

class GenerateRequest(BaseModel):
    genre_overrides: GenreOverrides = GenreOverrides()
    artist_overrides: ArtistOverrides = ArtistOverrides()
    playlist_name: Optional[str] = None
    song_type_filter: Optional[str] = None

@protected_router.post("/api/generate/{brand_id}")
def start_generate(brand_id: str, req: GenerateRequest = GenerateRequest(), user: dict = Depends(get_current_user)):
    brand = _get_brand_db(brand_id)
    if not brand or brand.get("user_id") != user["id"]:
        raise HTTPException(404, "Brand not found")
    tid = new_task()
    logger.warning("generate called: brand=%s genre_overrides=%s artist_overrides=%s", brand_id, req.genre_overrides.model_dump(), req.artist_overrides.model_dump())
    threading.Thread(target=_bg_playlist, args=(brand_id, tid, req.genre_overrides.model_dump(), req.playlist_name, req.song_type_filter, req.artist_overrides.model_dump()), daemon=True).start()
    return {"task_id": tid}


@protected_router.get("/api/generate/status/{task_id}")
def generation_status(task_id: str):
    t = _tasks.get(task_id)
    if not t:
        raise HTTPException(404, "Task not found")
    return t


@protected_router.put("/api/brands/{brand_id}/soundboard")
async def update_soundboard_targets(brand_id: str, req: Request, user: dict = Depends(get_current_user)):
    brand = _get_brand_db(brand_id)
    if not brand:
        raise HTTPException(404, "Brand not found")
    if brand.get("user_id") != user["id"]:
        raise HTTPException(403, "Not your brand")
    data = await req.json()
    targets: dict = data.get("targets", {})
    if not targets:
        raise HTTPException(400, "No targets provided")
    sb_result = brand.get("sound_board_result")
    if not sb_result:
        raise HTTPException(400, "No sound board found for this brand. Generate a sound board first.")
    sb = sb_result.setdefault("sound_board", {})
    for key, val in targets.items():
        sb[key] = val
    brand["sound_board_result"] = sb_result
    brand["last_updated"] = datetime.now(timezone.utc).isoformat()
    save_brands({brand_id: brand})
    return {"ok": True, "updated_keys": list(targets.keys())}


@protected_router.put("/api/brands/{brand_id}/dayparts")
async def update_dayparts(brand_id: str, req: Request, user: dict = Depends(get_current_user)):
    brand = _get_brand_db(brand_id)
    if not brand:
        raise HTTPException(404, "Brand not found")
    if brand.get("user_id") != user["id"]:
        raise HTTPException(403, "Not your brand")
    data = await req.json()
    incoming = data.get("day_parts", [])
    sb_result = brand.get("sound_board_result")
    if not sb_result:
        raise HTTPException(400, "No sound board found for this brand.")
    existing = sb_result.get("day_parts", [])
    TARGET_KEYS = [
        "energy_target", "valence_target", "tempo_target",
        "danceability_target", "acousticness_target",
        "instrumentalness_target", "loudness_target", "speechiness_target",
    ]
    for i, upd in enumerate(incoming):
        if i < len(existing):
            for k in TARGET_KEYS:
                if k in upd:
                    existing[i][k] = upd[k]
            # Re-center the hard filter window around the user-set target.
            # Use ±0.20 for energy/valence and ±20 BPM for tempo so that extreme
            # targets (e.g. energy=1.0 or BPM=180) still match enough songs from
            # the catalog without triggering full filter bypass.
            for base in ("energy", "valence"):
                t_key, mn_key, mx_key = f"{base}_target", f"{base}_min", f"{base}_max"
                if t_key in existing[i]:
                    t = float(existing[i][t_key])
                    existing[i][mn_key] = round(max(0.0, t - 0.20), 3)
                    existing[i][mx_key] = round(min(1.0, t + 0.20), 3)
            if "tempo_target" in existing[i]:
                t = float(existing[i]["tempo_target"])
                existing[i]["tempo_min"] = int(max(60.0, t - 20))
                existing[i]["tempo_max"] = int(min(200.0, t + 20))
    sb_result["day_parts"] = existing
    brand["sound_board_result"] = sb_result
    brand["last_updated"] = datetime.now(timezone.utc).isoformat()
    save_brands({brand_id: brand})
    return {"ok": True, "updated": len(incoming)}


@protected_router.put("/api/brands/{brand_id}/profile")
async def update_brand_profile(brand_id: str, req: Request, user: dict = Depends(get_current_user)):
    brand = _get_brand_db(brand_id)
    if not brand:
        raise HTTPException(404, "Brand not found")
    if brand.get("user_id") != user["id"]:
        raise HTTPException(403, "Not your brand")
    data = await req.json()
    PROFILE_KEYS = ["sincerity", "excitement", "competence", "sophistication", "ruggedness"]
    updates = {k: float(v) for k, v in data.items() if k in PROFILE_KEYS and v is not None}
    if not updates:
        raise HTTPException(400, "No valid profile keys provided")
    bp = brand.get("brand_profile") or {}
    bp.update(updates)
    brand["brand_profile"] = bp
    soundboard_updated = _apply_profile_targets_to_soundboard(brand)
    brand["last_updated"] = datetime.now(timezone.utc).isoformat()
    save_brands({brand_id: brand})
    return {
        "ok": True,
        "updated": updates,
        "soundboard_updated": soundboard_updated,
        "brand_profile": brand.get("brand_profile"),
        "sound_board_result": brand.get("sound_board_result"),
        "last_updated": brand.get("last_updated"),
    }


@protected_router.put("/api/brands/{brand_id}/genres")
async def update_genres(brand_id: str, req: Request, user: dict = Depends(get_current_user)):
    brand = _get_brand_db(brand_id)
    if not brand:
        raise HTTPException(404, "Brand not found")
    if brand.get("user_id") != user["id"]:
        raise HTTPException(403, "Not your brand")
    data = await req.json()
    brand["include_genres"] = data.get("include_genres", [])
    brand["exclude_genres"] = data.get("exclude_genres", [])
    if "song_type_filter" in data:
        brand["song_type_filter"] = data.get("song_type_filter") or None
    brand["last_updated"] = datetime.now(timezone.utc).isoformat()
    save_brands({brand_id: brand})
    return {"ok": True}


@protected_router.put("/api/brands/{brand_id}/artists")
async def update_artists(brand_id: str, req: Request, user: dict = Depends(get_current_user)):
    brand = _get_brand_db(brand_id)
    if not brand:
        raise HTTPException(404, "Brand not found")
    if brand.get("user_id") != user["id"]:
        raise HTTPException(403, "Not your brand")
    data = await req.json()
    brand["include_artists"] = data.get("include_artists", [])
    brand["exclude_artists"] = data.get("exclude_artists", [])
    brand["last_updated"] = datetime.now(timezone.utc).isoformat()
    save_brands({brand_id: brand})
    return {"ok": True}


def _recalc_daypart_stats(dp: dict) -> dict:
    tracks = dp.get("tracks", [])
    dp["track_count"] = len(tracks)
    dp["total_duration_seconds"] = sum(t.get("duration_seconds", 0) for t in tracks)
    if tracks:
        dp["avg_bfs"] = round(sum(t.get("bfs", 0) for t in tracks) / len(tracks), 3)
        dp["avg_mmr"] = round(sum(t.get("mmr_score", 0) for t in tracks) / len(tracks), 3)
    else:
        dp["avg_bfs"] = 0.0
        dp["avg_mmr"] = 0.0
    return dp


def _sync_playlist_songs(brand_id: str, user_id: str, day_parts: list, brand_name: str = "", brand_json: dict = None) -> None:
    unique_songs: dict[str, str] = {}
    for dp_r in day_parts:
        for track in dp_r.get("tracks", []):
            sid = track.get("song_id")
            if sid and sid not in unique_songs:
                unique_songs[sid] = track.get("title", "")
    now_iso = datetime.now(timezone.utc).isoformat()
    get_supabase().table("user_playlist_songs") \
        .delete().eq("user_id", user_id).eq("brand_id", brand_id).execute()
    if unique_songs:
        rows = [{"user_id": user_id, "brand_id": brand_id,
                 "brand_name": brand_name,
                 "song_id": sid, "song_name": name, "generated_at": now_iso,
                 "brand_json": brand_json}
                for sid, name in unique_songs.items()]
        get_supabase().table("user_playlist_songs").insert(rows).execute()


@protected_router.delete("/api/playlists/{brand_id}/tracks")
async def remove_tracks(brand_id: str, req: Request, user: dict = Depends(get_current_user)):
    brand = _get_brand_db(brand_id)
    if not brand:
        raise HTTPException(404, "Brand not found")
    if brand.get("user_id") != user["id"]:
        raise HTTPException(403, "Not your brand")
    pl = _get_playlist_db(brand_id)
    if not pl:
        raise HTTPException(404, "No playlist found")
    data = await req.json()
    dp_idx: int = data.get("day_part_index", -1)
    song_ids: list = data.get("song_ids", [])
    if not song_ids:
        raise HTTPException(400, "No song_ids provided")
    day_parts = pl.get("day_parts", [])
    if dp_idx < 0 or dp_idx >= len(day_parts):
        raise HTTPException(400, "Invalid day_part_index")
    dp = day_parts[dp_idx]
    remove_set = set(song_ids)
    dp["tracks"] = [t for t in dp.get("tracks", []) if t.get("song_id") not in remove_set]
    _recalc_daypart_stats(dp)
    try:
        _sync_playlist_songs(brand_id, user["id"], day_parts, brand.get("brand_name", ""), brand_json=brand)
    except Exception as e:
        logger.warning("Supabase sync failed after track removal: %s", e)
    try:
        _save_playlist_db(brand_id, user["id"], pl)
    except Exception as e:
        logger.warning("brand_playlists upsert failed after track removal: %s", e)
    # Re-resolve src for returned tracks
    for t in dp.get("tracks", []):
        t["src"] = _resolve_track_src(t)
    return {"ok": True, "day_part": dp}


@protected_router.post("/api/playlists/{brand_id}/tracks/replace")
async def replace_track(brand_id: str, req: Request, user: dict = Depends(get_current_user)):
    brand = _get_brand_db(brand_id)
    if not brand:
        raise HTTPException(404, "Brand not found")
    if brand.get("user_id") != user["id"]:
        raise HTTPException(403, "Not your brand")
    pl = _get_playlist_db(brand_id)
    if not pl:
        raise HTTPException(404, "No playlist found")
    data = await req.json()
    dp_idx: int = data.get("day_part_index", -1)
    song_id: str = data.get("song_id", "")
    if not song_id:
        raise HTTPException(400, "song_id required")
    day_parts = pl.get("day_parts", [])
    if dp_idx < 0 or dp_idx >= len(day_parts):
        raise HTTPException(400, "Invalid day_part_index")
    dp = day_parts[dp_idx]
    tracks = dp.get("tracks", [])
    track_idx = next((i for i, t in enumerate(tracks) if t.get("song_id") == song_id), None)
    if track_idx is None:
        raise HTTPException(404, "Track not found in day-part")
    original = tracks[track_idx]
    # Collect all song_ids already in the whole playlist
    used_ids = {t.get("song_id") for dp_r in day_parts for t in dp_r.get("tracks", [])}
    # Fetch catalog and filter
    from backend.db import fetch_all_songs as _fetch_all
    all_songs = _fetch_all()
    inputs = _pipeline_inputs(brand)
    filtered = apply_hard_filters(all_songs, dp, inputs)
    if not filtered:
        filtered = apply_exclusion_filters_only(all_songs, inputs)
    candidates = [s for s in filtered if s.get("song_id", s.get("id", "")) not in used_ids]
    if not candidates:
        raise HTTPException(404, "No replacement candidates available")
    # Score: 60% fit to day-part targets, 40% similarity to replaced song
    def _score(c: dict) -> float:
        return 0.6 * compute_relevance(c, dp) + 0.4 * compute_track_sim(c, original)
    best = max(candidates, key=_score)
    best.setdefault("song_id", best.get("id", ""))
    best["relevance_score"] = _score(best)
    replacement = _fmt_track(best)
    tracks[track_idx] = replacement
    dp["tracks"] = tracks
    _recalc_daypart_stats(dp)
    try:
        _sync_playlist_songs(brand_id, user["id"], day_parts, brand.get("brand_name", ""), brand_json=brand)
    except Exception as e:
        logger.warning("Supabase sync failed after track replace: %s", e)
    try:
        _save_playlist_db(brand_id, user["id"], pl)
    except Exception as e:
        logger.warning("brand_playlists upsert failed after track replace: %s", e)
    # Re-resolve src for returned tracks
    for t in dp.get("tracks", []):
        t["src"] = _resolve_track_src(t)
    return {"ok": True, "replacement": replacement, "day_part": dp}


@protected_router.get("/api/playlists/{brand_id}")
def get_playlist(brand_id: str):
    pl = _get_playlist_db(brand_id)
    if not pl:
        raise HTTPException(404, "No playlist found for this brand. Run generation first.")
    for dp in pl.get("day_parts", []):
        for t in dp.get("tracks", []):
            t["src"] = _resolve_track_src(t)
    return pl


@superadmin_router.get("/api/debug/songs")
def debug_songs():
    try:
        r = get_supabase().table("songs").select("id,title,artist,url").limit(3).execute()
        sample = [{"id": s["id"], "title": s.get("title", ""), "url": s.get("url", "")} for s in (r.data or [])]
        total = get_supabase().table("songs").select("id", count="exact").execute().count or 0
    except Exception as e:
        return {"error": str(e)}
    return {"total_songs": total, "sample": sample}


@app.get("/songs/{path:path}")
def serve_song(path: str):
    file_path = SONGS_DIR / path
    if not file_path.exists() or not file_path.is_file():
        raise HTTPException(404, "Song not found")
    return FileResponse(str(file_path), media_type="audio/mpeg")


# ─── Demo playlists ───────────────────────────────────────────────────────────

_DEMO_SPECS = [
    {
        "id":    "demo-cafe-am",
        "title": "Sunday morning, cafe mix.",
        "meta":  "Hospitality · AM · 36 tracks · 2 h 14 m",
        "n":     36,
        "genres": ["jazz", "acoustic", "ambient", "bossa nova", "folk", "classical", "cafe", "chillout", "lounge"],
        "tempo_max": 120,
    },
    {
        "id":    "demo-retail-pm",
        "title": "Saturday afternoon, flagship fashion store.",
        "meta":  "Retail · PM · 28 tracks · 1 h 52 m",
        "n":     28,
        "genres": ["indie pop", "pop", "electronic", "indie", "dance", "r&b", "hip hop", "hip-hop", "funk", "soul"],
        "tempo_min": 100,
    },
    {
        "id":    "demo-dining-eve",
        "title": "Friday evening, fine dining.",
        "meta":  "Hospitality · Evening · 22 tracks · 1 h 30 m",
        "n":     22,
        "genres": ["jazz", "classical", "ambient", "soul", "bossa nova", "lounge", "smooth jazz", "neo soul"],
    },
]

_demo_cache: list = []


def _fetch_demo_tracks(spec: dict) -> list[dict]:
    import random
    try:
        r = get_supabase().table("songs").select(
            "id,title,artist,genre,url,tempo_bpm,energy,duration_seconds"
        ).limit(500).execute()
        songs = r.data or []
    except Exception as e:
        logger.warning("_fetch_demo_tracks failed: %s", e)
        return []

    genres   = [g.lower() for g in spec.get("genres", [])]
    t_min    = spec.get("tempo_min", 0)
    t_max    = spec.get("tempo_max", 300)

    def genre_match(s):
        g = (s.get("genre") or "").lower()
        return any(genre in g for genre in genres)

    # genre + tempo filter
    filtered = [s for s in songs if genre_match(s) and t_min <= float(s.get("tempo_bpm") or 0) <= t_max]

    # relax tempo if too few
    if len(filtered) < spec["n"] // 2:
        filtered = [s for s in songs if genre_match(s)]

    # fall back to all songs
    if len(filtered) < spec["n"] // 2:
        filtered = songs[:]

    random.shuffle(filtered)
    selected = filtered[:spec["n"]]
    return [
        {
            "song_id":          s.get("id", ""),
            "title":            s.get("title", ""),
            "artist":           s.get("artist", ""),
            "genre":            s.get("genre", ""),
            "src":              _resolve_track_src(s),
            "duration_seconds": s.get("duration_seconds", 210),
        }
        for s in selected
    ]


@public_router.get("/api/demo")
def demo_playlists():
    """Public — returns 3 demo playlists for the landing page audio preview."""
    global _demo_cache
    if _demo_cache:
        return _demo_cache
    result = []
    for spec in _DEMO_SPECS:
        result.append({
            "id":     spec["id"],
            "title":  spec["title"],
            "meta":   spec["meta"],
            "tracks": _fetch_demo_tracks(spec),
        })
    _demo_cache = result
    return result


# ─── Routes: Public ───────────────────────────────────────────────────────────

@public_router.get("/api/config")
async def public_config():
    """Returns browser-safe Supabase config for the login page."""
    return {"supabase_url": SUPABASE_URL, "supabase_anon_key": SUPABASE_ANON_KEY}


@public_router.post("/api/auth/signup")
async def public_signup(request: Request):
    """Create account via admin API (auto-confirms email) then return session tokens."""
    import httpx
    body = await request.json()
    email    = (body.get("email") or "").strip()
    password = body.get("password") or ""
    metadata = {k: v for k, v in {
        "full_name": body.get("full_name", ""),
        "phone":     body.get("phone", ""),
    }.items() if v}

    if not email or not password:
        return JSONResponse({"error": "Email and password are required."}, status_code=400)

    async with httpx.AsyncClient(timeout=15) as client:
        # 1. Create user via admin API with email_confirm=True (skips confirmation email)
        create_resp = await client.post(
            f"{SUPABASE_URL}/auth/v1/admin/users",
            headers={"apikey": SUPABASE_SERVICE_KEY, "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
                     "Content-Type": "application/json"},
            json={"email": email, "password": password,
                  "email_confirm": True, "user_metadata": metadata},
        )
        if not create_resp.is_success:
            data = create_resp.json()
            msg  = data.get("error_description") or data.get("msg") or data.get("message") or "Sign up failed."
            if "already registered" in msg.lower() or create_resp.status_code == 422:
                msg = "This email is already registered. Please sign in."
            return JSONResponse({"error": msg}, status_code=400)

        # 2. Sign the new user in to get session tokens
        signin_resp = await client.post(
            f"{SUPABASE_URL}/auth/v1/token?grant_type=password",
            headers={"apikey": SUPABASE_ANON_KEY, "Content-Type": "application/json"},
            json={"email": email, "password": password},
        )
        if not signin_resp.is_success:
            return JSONResponse({"error": "Account created but sign-in failed. Please sign in manually."}, status_code=400)

        return JSONResponse(signin_resp.json())


# ─── Routes: Auth / Me ────────────────────────────────────────────────────────

@protected_router.get("/api/auth/me")
async def auth_me(
    user: dict = Depends(get_current_user),
    role: str  = Depends(get_current_role),
):
    return {"id": user["id"], "email": user.get("email", ""), "role": role}


@protected_router.get("/api/init")
def app_init(
    user: dict = Depends(get_current_user),
    role: str  = Depends(get_current_role),
):
    """Single bootstrap call: returns user info + brands in one round trip."""
    uid = user["id"]
    brands = list(get_brands(uid).values())
    brands = _reconcile_playlist_counts(uid, brands)
    return {
        "user":   {"id": uid, "email": user.get("email", ""), "role": role},
        "brands": brands,
    }


# ─── Routes: IAM (superadmin only) ───────────────────────────────────────────

class RoleUpdate(BaseModel):
    role: str


@superadmin_router.get("/api/iam/users")
async def iam_list_users():
    """List all Supabase Auth users with their roles."""
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{SUPABASE_URL}/auth/v1/admin/users?per_page=1000",
            headers={
                "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
                "apikey": SUPABASE_SERVICE_KEY,
            },
            timeout=10.0,
        )
    if resp.status_code != 200:
        raise HTTPException(500, "Could not fetch users from Supabase")

    auth_users = resp.json().get("users", [])
    roles_resp = get_supabase().table("user_roles").select("user_id,role").execute()
    roles_map  = {r["user_id"]: r["role"] for r in (roles_resp.data or [])}

    return [
        {
            "id":          u["id"],
            "email":       u.get("email", ""),
            "role":        roles_map.get(u["id"], "viewer"),
            "created_at":  u.get("created_at", ""),
            "last_sign_in": u.get("last_sign_in_at", ""),
        }
        for u in auth_users
    ]


@superadmin_router.put("/api/iam/users/{user_id}/role")
async def iam_update_role(user_id: str, body: RoleUpdate):
    if body.role not in {"superadmin", "admin", "viewer"}:
        raise HTTPException(400, "Invalid role. Must be superadmin, admin, or viewer")
    get_supabase().table("user_roles").upsert(
        {"user_id": user_id, "role": body.role},
        on_conflict="user_id",
    ).execute()
    return {"ok": True, "user_id": user_id, "role": body.role}


# ─── Mount routers ────────────────────────────────────────────────────────────

app.include_router(public_router)
app.include_router(protected_router)
app.include_router(superadmin_router)


if __name__ == "__main__":
    uvicorn.run("backend.api:app", host="127.0.0.1", port=8001, reload=False)
