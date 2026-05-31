"""
rag_retriever.py — BFS-based retrieval pipeline for fabPLAY v2.0

For each day-part:
  1. Fetch full song catalog from DB
  2. Apply hard filters (exclusions, tempo/energy/valence bounds)
  3. Min-candidate check with filter relaxation fallback
"""

import logging
import re

from api.db import fetch_all_songs, fetch_songs_filtered, fetch_songs_by_artist, fetch_songs_by_genre, get_supabase
from pipeline.mmr_scorer import compute_relevance

logger = logging.getLogger(__name__)


def _norm_genre(g: str) -> str:
    """Normalise a genre string for comparison: lowercase, spaces/slashes → underscores."""
    return (g or "").lower().replace(' ', '_').replace('/', '_').replace('-', '_')


def _has_playable_audio(track: dict) -> bool:
    """Return True when a track has a usable URL and non-trivial duration."""
    url = str(track.get("url") or track.get("src") or "").strip()
    if not url:
        return False
    # Accept absolute URLs, root-relative paths, and filename/path values.
    if not (
        url.startswith("http://")
        or url.startswith("https://")
        or url.startswith("/")
        or "/" in url
        or "." in url
    ):
        return False
    return float(track.get("duration_seconds") or 0) > 10

# ─── Hard-filter helpers ──────────────────────────────────────────────────────

def _is_explicit_proxy(track: dict) -> bool:
    """
    Proxy heuristic for explicit content:
    speechness > 0.66 AND energy > 0.80 suggests rap/explicit hip-hop.
    """
    return (
        float(track.get("speechness", 0)) > 0.66
        and float(track.get("energy", 0)) > 0.80
    )


def _matches_song_type(track: dict, song_type: str) -> bool:
    """Filter by vocal vs instrumental using song_type column, falling back to instrumentalness."""
    if song_type == "all":
        return True
    st = (track.get("song_type") or "").lower().strip()
    if st in ("vocal", "instrumental"):
        return st == song_type
    # Fallback: use instrumentalness threshold only when song_type column is absent
    inst = float(track.get("instrumentalness", 0.5))
    if song_type == "vocal":
        return inst < 0.5
    if song_type == "instrumental":
        return inst >= 0.5
    return True


def _apply_hard_filters(
    candidates: list[dict],
    day_part:   dict,
    inputs:     dict,
    relax:      float = 0.0,      # ±relaxation applied to energy/valence bounds
) -> list[dict]:
    """
    Filter candidate tracks by hard constraints.

    Hard filter criteria:
      ✗ artist in must_exclude_artists
      ✗ genre matches must_exclude_genres
      ✗ tempo_bpm outside [tempo_min, tempo_max]
      ✗ energy   outside [energy_min ± relax, energy_max ± relax]
      ✗ valence  outside [valence_min ± relax, valence_max ± relax]
      ✗ explicit proxy (when filter_explicit=True)
    """
    exclude_artists = _normalise_list(inputs.get("exclude_artists", ""))
    exclude_genres  = _normalise_list(inputs.get("exclude_genres", ""))
    filter_explicit = inputs.get("filter_explicit", True)
    song_type       = inputs.get("include_song_types", "all")

    tempo_min  = day_part.get("tempo_min",  60)
    tempo_max  = day_part.get("tempo_max", 180)
    energy_min = max(0.0, day_part.get("energy_min",  0.0) - relax)
    energy_max = min(1.0, day_part.get("energy_max",  1.0) + relax)
    valence_min = max(0.0, day_part.get("valence_min", 0.0) - relax)
    valence_max = min(1.0, day_part.get("valence_max", 1.0) + relax)

    filtered = []
    for track in candidates:
        artist = (track.get("artist") or "").lower()
        genre  = _norm_genre(track.get("genre"))
        tempo  = float(track.get("tempo_bpm", 120))
        energy = float(track.get("energy",    0.5))
        valence = float(track.get("valence",  0.5))

        if any(ea.lower() in artist for ea in exclude_artists if ea):
            continue
        if any(_norm_genre(eg) in genre for eg in exclude_genres if eg):
            continue
        if not (tempo_min <= tempo <= tempo_max):
            continue
        if not (energy_min <= energy <= energy_max):
            continue
        if not (valence_min <= valence <= valence_max):
            continue
        if filter_explicit and _is_explicit_proxy(track):
            continue
        if not _matches_song_type(track, song_type):
            continue

        filtered.append(track)

    return filtered


def _apply_exclusion_filters_only(
    candidates: list[dict],
    inputs:     dict,
) -> list[dict]:
    """
    Fallback filter: only apply artist/genre exclusions and explicit proxy.
    No energy/valence/tempo bounds — used when hard filters leave 0 candidates.
    """
    exclude_artists = _normalise_list(inputs.get("exclude_artists", ""))
    exclude_genres  = _normalise_list(inputs.get("exclude_genres", ""))
    filter_explicit = inputs.get("filter_explicit", True)
    song_type       = inputs.get("include_song_types", "all")

    filtered = []
    for track in candidates:
        artist = (track.get("artist") or "").lower()
        genre  = _norm_genre(track.get("genre"))
        if any(ea.lower() in artist for ea in exclude_artists if ea):
            continue
        if any(_norm_genre(eg) in genre for eg in exclude_genres if eg):
            continue
        if filter_explicit and _is_explicit_proxy(track):
            continue
        if not _matches_song_type(track, song_type):
            continue
        filtered.append(track)

    return filtered


from typing import Union


def _normalise_list(raw: Union[str, list]) -> list[str]:
    if isinstance(raw, list):
        return [str(x).strip() for x in raw if str(x).strip()]
    if not raw:
        return []
    return [x.strip() for x in str(raw).split(",") if x.strip()]


# ─── Analysis features (clap_audio_512, mood, arousal) ───────────────────────

def _fetch_analysis_features(song_ids: list[str], include_maest: bool = False) -> dict[str, dict]:
    """Fetch CLAP, mood, and arousal from analysis_song_features.

    Pass include_maest=True only for the song replacement / suggest-similar path.
    Playlist generation does not need MAEST vectors.
    """
    if not song_ids:
        return {}
    # analysis_song_features only has clap_audio_512 and maest_audio_768
    # arousal and mood_predicted_labels come from the songs table via fetch_all_songs()
    cols = "song_id,clap_audio_512"
    if include_maest:
        cols = "song_id,maest_audio_768,clap_audio_512"
    client = get_supabase()
    features: dict[str, dict] = {}
    batch_size = 500
    for i in range(0, len(song_ids), batch_size):
        batch = song_ids[i : i + batch_size]
        rows = (
            client.table("analysis_song_features")
            .select(cols)
            .in_("song_id", batch)
            .execute()
            .data or []
        )
        for row in rows:
            features[str(row["song_id"])] = {
                "maest_audio_768": row.get("maest_audio_768"),
                "clap_audio_512":  row.get("clap_audio_512"),
            }
    return features


def _attach_analysis_features(songs: list[dict], features: dict[str, dict]) -> None:
    # Only attaches embedding vectors from analysis_song_features.
    # arousal and mood_predicted_labels already exist on songs from fetch_all_songs().
    for song in songs:
        sid = str(song.get("song_id", song.get("id", "")))
        f = features.get(sid)
        if not f:
            continue
        if f.get("maest_audio_768"):
            song["maest_audio_768"] = f["maest_audio_768"]
        if f.get("clap_audio_512"):
            song["clap_audio_512"] = f["clap_audio_512"]


# ─── Music notes constraint parser ───────────────────────────────────────────

def _parse_music_notes_constraints(music_notes: str) -> dict:
    """
    Parse explicit numeric constraints from free-text music notes.

    Recognises patterns like:
      "energy >= 0.75", "energy above 0.75", ".75 energy", "0.75 energy"
      "bpm above 95", "95 bpm", "tempo above 95", "tempo >= 95"
      "valence >= 0.6", "valence above 0.6"

    Returns a dict with any of: energy_min, energy_max, tempo_min, tempo_max,
    valence_min, valence_max.  Only keys that were explicitly found are included.
    """
    if not music_notes:
        return {}

    text = music_notes.lower()
    constraints: dict = {}

    _NUM = r"(\d+(?:\.\d+)?)"
    _GTE = r"(?:>=|above|over|at\s+least|minimum|min)"
    _LTE = r"(?:<=|below|under|at\s+most|maximum|max)"

    # ── energy ────────────────────────────────────────────────────────────────
    # "energy >= 0.75" / "energy above 0.75"
    m = re.search(rf"energy\s*{_GTE}\s*{_NUM}", text)
    if m:
        constraints["energy_min"] = float(m.group(1))

    m = re.search(rf"energy\s*{_LTE}\s*{_NUM}", text)
    if m:
        constraints["energy_max"] = float(m.group(1))

    # "0.75 energy" / ".75 energy"
    m = re.search(rf"{_NUM}\s+energy", text)
    if m and "energy_min" not in constraints and "energy_max" not in constraints:
        constraints["energy_min"] = float(m.group(1))

    # ── tempo / bpm ───────────────────────────────────────────────────────────
    m = re.search(rf"(?:bpm|tempo)\s*{_GTE}\s*{_NUM}", text)
    if m:
        constraints["tempo_min"] = float(m.group(1))

    m = re.search(rf"(?:bpm|tempo)\s*{_LTE}\s*{_NUM}", text)
    if m:
        constraints["tempo_max"] = float(m.group(1))

    # "95 bpm" / "95 tempo"
    m = re.search(rf"{_NUM}\s+(?:bpm|tempo)", text)
    if m and "tempo_min" not in constraints and "tempo_max" not in constraints:
        constraints["tempo_min"] = float(m.group(1))

    # ── valence ───────────────────────────────────────────────────────────────
    m = re.search(rf"valence\s*{_GTE}\s*{_NUM}", text)
    if m:
        constraints["valence_min"] = float(m.group(1))

    m = re.search(rf"valence\s*{_LTE}\s*{_NUM}", text)
    if m:
        constraints["valence_max"] = float(m.group(1))

    if constraints:
        logger.info("Music-notes hard constraints parsed: %s", constraints)
    return constraints


def _apply_music_notes_constraints(songs: list[dict], constraints: dict) -> list[dict]:
    """Drop any song that violates a parsed music-notes hard constraint."""
    if not constraints:
        return songs

    energy_min  = constraints.get("energy_min")
    energy_max  = constraints.get("energy_max")
    tempo_min   = constraints.get("tempo_min")
    tempo_max   = constraints.get("tempo_max")
    valence_min = constraints.get("valence_min")
    valence_max = constraints.get("valence_max")

    result = []
    for t in songs:
        energy = float(t.get("energy", 0.5))
        tempo  = float(t.get("tempo_bpm", 120))
        valence = float(t.get("valence", 0.5))

        if energy_min  is not None and energy  < energy_min:
            continue
        if energy_max  is not None and energy  > energy_max:
            continue
        if tempo_min   is not None and tempo   < tempo_min:
            continue
        if tempo_max   is not None and tempo   > tempo_max:
            continue
        if valence_min is not None and valence < valence_min:
            continue
        if valence_max is not None and valence > valence_max:
            continue
        result.append(t)

    return result


# ─── Main retriever ───────────────────────────────────────────────────────────

# Top N songs passed to MMR per day-part (sorted by relevance).
# 5 day-parts × 75 tracks = 375 max needed; 1200 gives comfortable headroom.
MAX_CANDIDATES = 1200


def retrieve_candidates(
    brand_profile:   dict,
    day_part_params: dict,
    inputs:          dict,
    used_song_ids:   set | None = None,
) -> tuple[list[dict], list[dict], dict]:
    """
    Retrieval for one day-part.

    Considers the full song catalog — only artist/genre exclusions and explicit
    proxy are applied as hard filters. Energy/tempo/valence bounds are NOT used
    here; MMR relevance scoring already ranks tracks by audio-feature closeness
    to the day-part target, so hard bounds are redundant and only shrink the pool.

    Returns:
        (candidates, all_playable, stats_dict)
        candidates   — top MAX_CANDIDATES by relevance, fresh tracks first
        all_playable — full exclusion-filtered+playable catalog (spillover source)
        stats_dict   — {'filtered_count': int, 'skipped': bool}
    """
    # 1. Fetch full catalog
    all_songs = fetch_all_songs()
    for s in all_songs:
        if "song_id" not in s:
            s["song_id"] = s.get("id")

    # 2. Only apply exclusion filters (artists, genres, explicit) + playable check.
    all_playable = [
        s for s in _apply_exclusion_filters_only(all_songs, inputs)
        if _has_playable_audio(s)
    ]

    # 2b. Enforce any explicit numeric constraints from music_notes as hard filters.
    mn_constraints = _parse_music_notes_constraints(inputs.get("music_notes", ""))
    if mn_constraints:
        before = len(all_playable)
        all_playable = _apply_music_notes_constraints(all_playable, mn_constraints)
        logger.info(
            "Music-notes constraints reduced pool: %d → %d tracks", before, len(all_playable)
        )

    if not all_playable:
        return [], [], {"filtered_count": 0, "skipped": True}

    # 3. Sort by relevance to this day-part; put fresh (unused) tracks first so
    #    the MAX_CANDIDATES window is dominated by songs not yet in earlier day-parts.
    all_playable.sort(key=lambda t: compute_relevance(t, day_part_params), reverse=True)
    if used_song_ids:
        fresh = [t for t in all_playable if t.get("song_id") not in used_song_ids]
        stale = [t for t in all_playable if t.get("song_id") in used_song_ids]
        candidates = (fresh + stale)[:MAX_CANDIDATES]
    else:
        candidates = all_playable[:MAX_CANDIDATES]

    # 4. Attach clap_audio_512, mood_predicted_labels, arousal to candidates.
    song_ids = [str(t.get("song_id", t.get("id", ""))) for t in candidates]
    analysis = _fetch_analysis_features(song_ids)
    if analysis:
        _attach_analysis_features(candidates, analysis)
        logger.debug("Analysis features attached for %d/%d candidates", len(analysis), len(candidates))

    return candidates, all_playable, {"filtered_count": len(candidates), "skipped": False}


def fetch_must_include_tracks(
    must_include_artists: list[str],
    candidates:           list[dict],
    target_count:         int,
) -> list[dict]:
    """
    Pre-select tracks for must-include artists.
    - First looks in candidates pool.
    - Falls back to direct SQL query if artist not found.
    - Capped at 15% of target playlist length.

    Returns list of track dicts to be injected at head of final playlist.
    """
    cap = max(1, int(target_count * 0.15))
    selected: list[dict] = []

    for artist_name in must_include_artists:
        if not artist_name.strip():
            continue

        # Check candidates pool first
        matches = [
            t for t in candidates
            if artist_name.lower() in (t.get("artist") or "").lower()
        ]

        # Keep only playable tracks from the candidate pool.
        matches = [t for t in matches if _has_playable_audio(t)]

        if not matches:
            # SQL fallback
            sql_tracks = fetch_songs_by_artist(artist_name)
            if not sql_tracks:
                logger.warning("Must-include artist '%s' not found anywhere. Skipping.", artist_name)
                continue
            for t in sql_tracks:
                t["similarity"] = 0.0   # no ANN similarity for SQL-fetched tracks
                t["song_id"]    = t.get("song_id", t.get("id"))
            matches = [t for t in sql_tracks if _has_playable_audio(t)]
            if not matches:
                logger.warning("Must-include artist '%s' has no playable URLs. Skipping.", artist_name)
                continue

        # Take best 1–2 tracks per must-include artist
        selected.extend(matches[:2])
        if len(selected) >= cap:
            break

    return selected[:cap]


def fetch_must_include_genre_tracks(
    include_genres: list[str],
    candidates:     list[dict],
    target_count:   int,
) -> list[dict]:
    """
    Pre-select tracks for must-include genres.
    - First looks in candidates pool.
    - Falls back to direct SQL query if genre not found.
    - Capped at 30% of target playlist length.

    Returns list of track dicts to be injected into candidates.
    """
    if not include_genres:
        return []

    cap = max(1, int(target_count * 0.30))
    selected: list[dict] = []
    seen_ids: set = set()

    for genre_name in include_genres:
        if not genre_name.strip():
            continue

        # Check candidates pool first
        matches = [
            t for t in candidates
            if _norm_genre(genre_name) in _norm_genre(t.get("genre"))
            and t.get("song_id") not in seen_ids
        ]

        # Keep only playable tracks from the candidate pool.
        matches = [t for t in matches if _has_playable_audio(t)]

        if not matches:
            # SQL fallback
            sql_tracks = fetch_songs_by_genre(genre_name)
            if not sql_tracks:
                logger.warning("Must-include genre '%s' not found anywhere. Skipping.", genre_name)
                continue
            for t in sql_tracks:
                t["similarity"] = 0.0
                t["song_id"] = t.get("song_id", t.get("id"))
            matches = [
                t for t in sql_tracks
                if t.get("song_id") not in seen_ids and _has_playable_audio(t)
            ]
            if not matches:
                logger.warning("Must-include genre '%s' has no playable URLs. Skipping.", genre_name)
                continue

        # Take up to 4 tracks per genre
        for t in matches[:4]:
            selected.append(t)
            seen_ids.add(t.get("song_id"))

        if len(selected) >= cap:
            break

    return selected[:cap]


# ─── Public aliases for use outside this module ───────────────────────────────

def apply_hard_filters(candidates: list, day_part: dict, inputs: dict, relax: float = 0.0) -> list:
    return _apply_hard_filters(candidates, day_part, inputs, relax)


def apply_exclusion_filters_only(candidates: list, inputs: dict) -> list:
    return _apply_exclusion_filters_only(candidates, inputs)
