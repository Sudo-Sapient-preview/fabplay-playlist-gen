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
import unicodedata
from typing import Optional
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote
import xml.etree.ElementTree as ET

import httpx
import uvicorn
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, UploadFile, File, Request, Depends, APIRouter
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv
from openai import AzureOpenAI

from backend.db import get_supabase, run_startup_checks
from brand_pipeline.brand_analysis import get_brand_profile
from brand_pipeline.sound_board import get_sound_board, apply_segment_adjustments
from pipeline.rag_retriever import retrieve_candidates, fetch_must_include_tracks, fetch_must_include_genre_tracks
from pipeline.mmr_scorer import mmr_select
from brand_pipeline.day_part_templates import get_template, get_day_part_hours, target_track_count

load_dotenv()
logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

MMR_LAMBDA = float(os.getenv("MMR_LAMBDA", "0.7"))

# ─── Auth config ──────────────────────────────────────────────────────────────

SUPABASE_URL      = os.getenv("SUPABASE_URL", "")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY", "")
SUPABASE_ANON_KEY = os.getenv("SUPABASE_ANON_KEY", "")
_bearer = HTTPBearer()


async def get_current_user(
    creds: HTTPAuthorizationCredentials = Depends(_bearer),
) -> dict:
    """Validate a Supabase JWT by calling /auth/v1/user. Returns the user dict."""
    token = creds.credentials
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{SUPABASE_URL}/auth/v1/user",
                headers={
                    "Authorization": f"Bearer {token}",
                    "apikey": SUPABASE_SERVICE_KEY,
                },
                timeout=10.0,
            )
    except (httpx.ConnectTimeout, httpx.ReadTimeout, httpx.ConnectError) as e:
        raise HTTPException(status_code=503, detail="Auth service unavailable")
    if resp.status_code != 200:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    return resp.json()


async def get_current_role(user: dict = Depends(get_current_user)) -> str:
    """Fetch this user's role from the user_roles table. Returns 'viewer' if no row found."""
    result = (
        get_supabase()
        .table("user_roles")
        .select("role")
        .eq("user_id", user["id"])
        .maybe_single()
        .execute()
    )
    return result.data["role"] if (result and result.data) else "viewer"


async def require_superadmin(role: str = Depends(get_current_role)):
    if role != "superadmin":
        raise HTTPException(status_code=403, detail="Superadmin access required")

@asynccontextmanager
async def lifespan(app: FastAPI):
    run_startup_checks(exit_on_fatal=False)
    idx = _build_file_index()
    logger.warning(f"Song index built: {len(idx)} files found in {SONGS_DIR}")
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
SONGS_BASE_URL = os.getenv("SONGS_BASE_URL", "").rstrip("/")
SONGS_MANIFEST = Path(__file__).parent.parent / "songs_manifest.json"
_file_index: Optional[dict] = None


def _build_blob_index(base_url: str) -> dict:
    """
    Build {filename -> blob relative path} by listing the Azure Blob container.
    This lets us resolve DB filenames to nested blob keys like:
      "folder/subfolder/track.mp3"
    """
    idx: dict[str, str] = {}
    marker = ""
    # Azure list API (public container): ?restype=container&comp=list
    while True:
        list_url = f"{base_url}/songs?restype=container&comp=list"
        if marker:
            list_url += f"&marker={quote(marker, safe='')}"
        resp = httpx.get(list_url, timeout=20.0)
        if resp.status_code != 200:
            logger.warning("Blob list failed (%s): %s", resp.status_code, list_url)
            break

        try:
            root = ET.fromstring(resp.text)
        except ET.ParseError:
            logger.warning("Blob list XML parse failed")
            break

        blobs = root.find("Blobs")
        if blobs is not None:
            for blob in blobs.findall("Blob"):
                name_el = blob.find("Name")
                if name_el is None or not name_el.text:
                    continue
                rel = name_el.text.strip().replace("\\", "/")
                fname = rel.split("/")[-1]
                if fname.lower().endswith(".mp3"):
                    idx[fname] = rel

        next_marker_el = root.find("NextMarker")
        marker = (next_marker_el.text or "").strip() if next_marker_el is not None else ""
        if not marker:
            break
    return idx


def _build_file_index() -> dict:
    global _file_index
    if _file_index is not None:
        return _file_index
    _file_index = {}
    if SONGS_DIR.exists():
        for f in SONGS_DIR.rglob("*.mp3"):
            rel = f.relative_to(SONGS_DIR)
            _file_index[f.name] = str(rel)
    elif SONGS_MANIFEST.exists():
        import json as _json
        _file_index = _json.loads(SONGS_MANIFEST.read_text(encoding="utf-8"))
        logger.warning(f"Loaded songs manifest: {len(_file_index)} entries from {SONGS_MANIFEST}")
    elif SONGS_BASE_URL and "blob.core.windows.net" in SONGS_BASE_URL:
        _file_index = _build_blob_index(SONGS_BASE_URL)
        logger.warning(f"Built blob songs index: {len(_file_index)} entries from {SONGS_BASE_URL}/songs")
    return _file_index


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s)
    s = s.replace("\u2019", "'").replace("\u2018", "'")
    return " ".join(s.split()).lower()


def _songs_url(rel_path: str) -> str:
    path = rel_path.replace("\\", "/")
    encoded = "/".join(quote(seg, safe="") for seg in path.split("/"))
    if SONGS_BASE_URL:
        return f"{SONGS_BASE_URL}/songs/{encoded}"
    return "/songs/" + encoded


def _resolve_track_src(track: dict) -> str:
    idx = _build_file_index()
    fname = track.get("filename") or ""
    if fname and fname in idx:
        return _songs_url(idx[fname])
    # Fallback for VM streaming mode when no local songs index/manifest exists.
    # Assumes files are directly addressable by filename under SONGS_BASE_URL/songs/.
    if fname and SONGS_BASE_URL and not idx:
        return _songs_url(fname)
    if fname:
        fn = _norm(fname)
        for name, path in idx.items():
            if _norm(name) == fn:
                return _songs_url(path)
    title = (track.get("title") or track.get("name") or "").strip().rstrip("_").strip()
    for name, path in idx.items():
        if name.startswith(title):
            return _songs_url(path)
    tn = _norm(title)
    for name, path in idx.items():
        if _norm(name).startswith(tn):
            return _songs_url(path)
    display = title.split(" _ ")[0].split("_")[0].strip().lower()
    if len(display) > 4:
        for name, path in idx.items():
            if name.lower().startswith(display):
                return _songs_url(path)
    return ""


# ─── Persistence (JSON files) ─────────────────────────────────────────────────

BRANDS_FILE    = Path("data/brands_store.json")
PLAYLISTS_FILE = Path("data/playlists_store.json")


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


def get_brands()    -> dict: return _load(BRANDS_FILE,    {})
def get_playlists() -> dict: return _load(PLAYLISTS_FILE, {})
def save_brands(b):    _save(BRANDS_FILE,    b)
def save_playlists(p): _save(PLAYLISTS_FILE, p)


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
        "title":            t.get("title", ""),
        "artist":           t.get("artist", ""),
        "genre":            t.get("genre", ""),
        "filename":         t.get("filename", ""),
        "src":              _resolve_track_src(t),
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
        brands = get_brands()
        brand  = brands.get(brand_id)
        if not brand:
            task_error(tid, f"Brand {brand_id} not found"); return

        task_progress(tid, 10, "Analysing brand identity...")
        cc, dep = chat_client()
        inputs  = _pipeline_inputs(brand)

        bp = get_brand_profile(inputs, cc, dep)
        bp.setdefault("brand_name",           brand["brand_name"])
        bp.setdefault("customer_description", brand.get("customer_description", ""))
        bp.setdefault("price_positioning",    brand.get("customer_segment", "mid_range"))

        task_progress(tid, 55, "Generating sound board parameters...")
        sb = get_sound_board(bp, brand.get("category", "cafe"), cc, dep)
        sb = apply_segment_adjustments(sb, brand.get("customer_segment", "mid_range"))

        brands = get_brands()
        brands[brand_id].update({
            "brand_profile":      bp,
            "sound_board_result": sb,
            "status":             "active",
            "last_updated":       datetime.now(timezone.utc).isoformat(),
        })
        save_brands(brands)
        log_activity("Sound board generated", brand["brand_name"])
        task_progress(tid, 100, "Sound board ready.")
        task_done(tid)

    except Exception as e:
        logger.exception("Sound board failed")
        task_error(tid, str(e))


# ─── Background: Playlist generation (MMR) ───────────────────────────────────

def _bg_playlist(brand_id: str, tid: str, genre_overrides: Optional[dict] = None):
    try:
        brands = get_brands()
        brand  = brands.get(brand_id)
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

        if not bp or not sb_result:
            task_progress(tid, 5, "Generating brand profile...")
            cc, dep = chat_client()
            inputs  = _apply_genre_overrides(_pipeline_inputs(brand))
            bp = get_brand_profile(inputs, cc, dep)
            bp.setdefault("brand_name",           brand["brand_name"])
            bp.setdefault("customer_description", brand.get("customer_description", ""))
            bp.setdefault("price_positioning",    brand.get("customer_segment", "mid_range"))

            task_progress(tid, 20, "Generating sound board...")
            sb_result = get_sound_board(bp, brand.get("category", "cafe"), cc, dep)
            sb_result = apply_segment_adjustments(sb_result, brand.get("customer_segment", "mid_range"))

            brands = get_brands()
            brands[brand_id]["brand_profile"]      = bp
            brands[brand_id]["sound_board_result"] = sb_result
            save_brands(brands)

        task_progress(tid, 30, "Starting MMR selection...")
        inputs    = _apply_genre_overrides(_pipeline_inputs(brand))
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

            candidates, stats = retrieve_candidates(
                brand_profile   = bp,
                day_part_params = dp,
                inputs          = inputs,
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

            playlist = mmr_select(
                candidates     = candidates,
                must_include   = must_inc,
                dp             = dp,
                target_count   = target_n,
                target_seconds = target_seconds,
                lam            = MMR_LAMBDA,
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

        playlists = get_playlists()
        playlists[brand_id] = playlist_result
        save_playlists(playlists)

        brands = get_brands()
        brands[brand_id]["playlist_count"] = brands[brand_id].get("playlist_count", 0) + 1
        brands[brand_id]["last_updated"]   = datetime.now(timezone.utc).isoformat()
        brands[brand_id]["status"]         = "active"
        save_brands(brands)

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
def stats():
    brands    = get_brands()
    playlists = get_playlists()
    active    = sum(1 for b in brands.values() if b.get("status") == "active")
    mmr_vals  = [
        dp["avg_bfs"]
        for pl in playlists.values()
        for dp in pl.get("day_parts", [])
        if dp.get("avg_bfs", 0) > 0
    ]
    return {
        "total_brands":         len(brands),
        "active_sound_boards":  active,
        "playlists_this_month": len(playlists),
        "avg_bfs":              round(sum(mmr_vals) / len(mmr_vals), 3) if mmr_vals else 0.0,
    }


@protected_router.get("/api/activity")
def activity():
    return [{"time_ago": _time_ago(a["timestamp"]), **a} for a in _activity[:10]]


# ─── Routes: Brands ───────────────────────────────────────────────────────────

@protected_router.get("/api/brands")
def list_brands(user: dict = Depends(get_current_user)):
    uid = user["id"]
    return [b for b in get_brands().values() if b.get("user_id") == uid]


@protected_router.post("/api/brands")
async def create_brand(req: Request, user: dict = Depends(get_current_user)):
    data     = await req.json()
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
        "music_notes":          data.get("music_notes", ""),
        "asset_analysis":       data.get("asset_analysis", ""),
        "has_brand_guidelines": data.get("has_brand_guidelines", False),
        "status":               "setup",
        "playlist_count":       0,
        "last_updated":         now,
        "brand_profile":        None,
        "sound_board_result":   None,
    }
    brands = get_brands()
    brands[brand_id] = brand
    save_brands(brands)
    log_activity("Brand created", brand["brand_name"])
    return brand


@protected_router.delete("/api/brands/{brand_id}")
def delete_brand(brand_id: str, user: dict = Depends(get_current_user)):
    brands = get_brands()
    if brand_id not in brands:
        raise HTTPException(404, "Brand not found")
    if brands[brand_id].get("user_id") != user["id"]:
        raise HTTPException(403, "Not your brand")
    name = brands.pop(brand_id)["brand_name"]
    save_brands(brands)
    playlists = get_playlists()
    playlists.pop(brand_id, None)
    save_playlists(playlists)
    log_activity("Brand deleted", name)
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
            MUSIC_EXTRACT_PROMPT = """\
You are a brand strategist. Based on this brand asset analysis, return ONLY a valid JSON object:
{
  "recommended_genres": ["genre1", "genre2", "genre3"],
  "avoid_genres": ["genre1", "genre2"],
  "music_notes": "Brief note about music direction (1-2 sentences)"
}
Only include genres from this list: Pop, House, Chillout, Indie, Lounge, Afro Pop, Dance, Rock, Europop, Ambient, Orchestral, Electronic, Electropop, Deep House, Jazz, Tropical House, Folk, Blues, Bollywood, Soul, R&B, Hip Hop, Funk, Indie Rock, Indie Pop"""
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
async def upload_assets(brand_id: str, files: list[UploadFile] = File(...)):
    import base64, fitz

    brands = get_brands()
    if brand_id not in brands:
        raise HTTPException(404, "Brand not found")

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
        brands = get_brands()
        brands[brand_id]["asset_analysis"]      = "\n\n".join(analyses)
        brands[brand_id]["has_brand_guidelines"] = has_brand_guidelines
        brands[brand_id]["brand_profile"]        = None
        brands[brand_id]["sound_board_result"]   = None
        save_brands(brands)

    return {"analyzed": len(analyses), "has_brand_guidelines": has_brand_guidelines}


# ─── Routes: Catalog ──────────────────────────────────────────────────────────

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
            "id,title,artist,genre,tempo_bpm,energy,valence,"
            "danceability,acousticness,instrumentalness,loudness,speechness"
        ).limit(50).execute()
        return {"songs": [
            {
                "id":               s.get("id"),
                "title":            s.get("title", ""),
                "artist":           s.get("artist", ""),
                "genre":            s.get("genre", ""),
                "tempo_bpm":        s.get("tempo_bpm", 120),
                "energy":           s.get("energy", 0.5),
                "valence":          s.get("valence", 0.5),
                "danceability":     s.get("danceability", 0.5),
                "acousticness":     s.get("acousticness", 0.4),
                "instrumentalness": s.get("instrumentalness", 0.3),
                "loudness":         s.get("loudness", -8.0),
                "speechiness":      s.get("speechness", 0.1),
            }
            for s in (r.data or [])
        ]}
    except Exception as e:
        return {"songs": [], "error": str(e)}


# ─── Routes: Quick Analyze ────────────────────────────────────────────────────

QUICK_ANALYZE_PROMPT = """\
You are a brand strategist. Given a brand name, business category, and optional website, return ONLY a valid JSON object:
{
  "customer_segment": "budget"|"mid_range"|"premium"|"luxury",
  "age_min": int,
  "age_max": int,
  "brand_description": "2-3 sentence brand narrative",
  "suggested_activities": ["activity1", "activity2", "activity3", "activity4", "activity5"],
  "suggested_customer_types": ["type1", "type2", "type3", "type4", "type5"],
  "suggested_lifestyle": ["tag1", "tag2", "tag3", "tag4", "tag5"]
}
IMPORTANT: suggested_activities, suggested_customer_types, and suggested_lifestyle must be based on the BUSINESS CATEGORY only (e.g. hotel, cafe, retail) — not the brand name.
The brand name is only used to infer customer_segment and brand_description.
suggested_activities: 4-6 things customers typically do in this type of space (e.g. for hotel: "Check in and relax", "Dine at the restaurant", "Use pool or spa", "Attend meetings")
suggested_customer_types: 4-6 types of people who visit this type of place (e.g. for hotel: "Business travelers", "Couples on holiday", "Families", "Solo travelers")
suggested_lifestyle: 4-6 lifestyle descriptors typical for this type of venue (e.g. for hotel: "Urban professional", "Leisure traveler", "Health-conscious")"""


@protected_router.post("/api/quick-analyze")
async def quick_analyze(req: Request):
    data = await req.json()
    brand_name  = data.get("brand_name", "")
    website_url = data.get("website_url", "")
    category    = data.get("category", "")
    try:
        cc, dep = chat_client()
        resp = cc.chat.completions.create(
            model=dep,
            max_completion_tokens=600,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": QUICK_ANALYZE_PROMPT},
                {"role": "user",   "content": f"Brand: {brand_name}\nCategory: {category or 'not provided'}\nWebsite: {website_url or 'not provided'}"},
            ],
        )
        return json.loads(resp.choices[0].message.content)
    except Exception as e:
        raise HTTPException(500, str(e))


# ─── Routes: Generation ───────────────────────────────────────────────────────

@protected_router.post("/api/soundboard/{brand_id}")
def start_soundboard(brand_id: str):
    if brand_id not in get_brands():
        raise HTTPException(404, "Brand not found")
    tid = new_task()
    threading.Thread(target=_bg_soundboard, args=(brand_id, tid), daemon=True).start()
    return {"task_id": tid}


class GenreOverrides(BaseModel):
    include: list[str] = []
    exclude: list[str] = []

class GenerateRequest(BaseModel):
    genre_overrides: GenreOverrides = GenreOverrides()

@protected_router.post("/api/generate/{brand_id}")
def start_generate(brand_id: str, req: GenerateRequest = GenerateRequest()):
    if brand_id not in get_brands():
        raise HTTPException(404, "Brand not found")
    tid = new_task()
    logger.warning("generate called: brand=%s genre_overrides=%s", brand_id, req.genre_overrides.model_dump())
    threading.Thread(target=_bg_playlist, args=(brand_id, tid, req.genre_overrides.model_dump()), daemon=True).start()
    return {"task_id": tid}


@protected_router.get("/api/generate/status/{task_id}")
def generation_status(task_id: str):
    t = _tasks.get(task_id)
    if not t:
        raise HTTPException(404, "Task not found")
    return t


@protected_router.put("/api/brands/{brand_id}/soundboard")
async def update_soundboard_targets(brand_id: str, req: Request):
    brands = get_brands()
    if brand_id not in brands:
        raise HTTPException(404, "Brand not found")
    data = await req.json()
    targets: dict = data.get("targets", {})
    if not targets:
        raise HTTPException(400, "No targets provided")
    brand = brands[brand_id]
    sb_result = brand.get("sound_board_result")
    if not sb_result:
        raise HTTPException(400, "No sound board found for this brand. Generate a sound board first.")
    sb = sb_result.setdefault("sound_board", {})
    for key, val in targets.items():
        sb[key] = val
    for dp in sb_result.get("day_parts", []):
        for key, val in targets.items():
            dp[key] = val
    brands[brand_id]["sound_board_result"] = sb_result
    brands[brand_id]["last_updated"] = datetime.now(timezone.utc).isoformat()
    save_brands(brands)
    return {"ok": True, "updated_keys": list(targets.keys())}


@protected_router.put("/api/brands/{brand_id}/dayparts")
async def update_dayparts(brand_id: str, req: Request):
    brands = get_brands()
    if brand_id not in brands:
        raise HTTPException(404, "Brand not found")
    data = await req.json()
    incoming = data.get("day_parts", [])
    sb_result = brands[brand_id].get("sound_board_result")
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
    sb_result["day_parts"] = existing
    brands[brand_id]["sound_board_result"] = sb_result
    brands[brand_id]["last_updated"] = datetime.now(timezone.utc).isoformat()
    save_brands(brands)
    return {"ok": True, "updated": len(incoming)}


@protected_router.put("/api/brands/{brand_id}/profile")
async def update_brand_profile(brand_id: str, req: Request):
    brands = get_brands()
    if brand_id not in brands:
        raise HTTPException(404, "Brand not found")
    data = await req.json()
    PROFILE_KEYS = ["sincerity", "excitement", "competence", "sophistication", "ruggedness"]
    updates = {k: float(v) for k, v in data.items() if k in PROFILE_KEYS and v is not None}
    if not updates:
        raise HTTPException(400, "No valid profile keys provided")
    bp = brands[brand_id].get("brand_profile") or {}
    bp.update(updates)
    brands[brand_id]["brand_profile"] = bp
    soundboard_updated = _apply_profile_targets_to_soundboard(brands[brand_id])
    brands[brand_id]["last_updated"] = datetime.now(timezone.utc).isoformat()
    save_brands(brands)
    return {
        "ok": True,
        "updated": updates,
        "soundboard_updated": soundboard_updated,
        "brand_profile": brands[brand_id].get("brand_profile"),
        "sound_board_result": brands[brand_id].get("sound_board_result"),
        "last_updated": brands[brand_id].get("last_updated"),
    }


@protected_router.get("/api/playlists/{brand_id}")
def get_playlist(brand_id: str):
    pl = get_playlists().get(brand_id)
    if not pl:
        raise HTTPException(404, "No playlist found for this brand. Run generation first.")
    for dp in pl.get("day_parts", []):
        for t in dp.get("tracks", []):
            t["src"] = _resolve_track_src(t)
    return pl


@superadmin_router.get("/api/debug/songs")
def debug_songs():
    idx = _build_file_index()
    sample = []
    for name, path in list(idx.items())[:3]:
        sample.append({"filename": name, "src": _songs_url(path)})
    return {
        "songs_dir":        str(SONGS_DIR),
        "songs_dir_exists": SONGS_DIR.exists(),
        "indexed_files":    len(idx),
        "sample":           sample,
    }


@app.get("/songs/{path:path}")
def serve_song(path: str):
    file_path = SONGS_DIR / path
    if not file_path.exists() or not file_path.is_file():
        raise HTTPException(404, "Song not found")
    return FileResponse(str(file_path), media_type="audio/mpeg")


# ─── Routes: Public ───────────────────────────────────────────────────────────

@public_router.get("/api/config")
async def public_config():
    """Returns browser-safe Supabase config for the login page."""
    return {"supabase_url": SUPABASE_URL, "supabase_anon_key": SUPABASE_ANON_KEY}


# ─── Routes: Auth / Me ────────────────────────────────────────────────────────

@protected_router.get("/api/auth/me")
async def auth_me(
    user: dict = Depends(get_current_user),
    role: str  = Depends(get_current_role),
):
    return {"id": user["id"], "email": user.get("email", ""), "role": role}


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
