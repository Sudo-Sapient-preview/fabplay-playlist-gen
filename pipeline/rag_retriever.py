"""
rag_retriever.py — BFS-based retrieval pipeline for fabPLAY v2.0

For each day-part:
  1. Fetch full song catalog from DB
  2. Apply hard filters (exclusions, tempo/energy/valence bounds)
  3. Min-candidate check with filter relaxation fallback
"""

import logging

from backend.db import fetch_all_songs, fetch_songs_filtered, fetch_songs_by_artist, fetch_songs_by_genre
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

# Genres/themes that must never appear in any generated playlist
_BANNED_KEYWORDS = {"christmas", "wedding", "bollywood", "diwali", "festival"}


def _is_banned(track: dict) -> bool:
    """Return True if a track's genre or title contains a banned keyword."""
    genre = (track.get("genre") or "").lower()
    title = (track.get("title") or "").lower()
    return any(kw in genre or kw in title for kw in _BANNED_KEYWORDS)


def _is_explicit_proxy(track: dict) -> bool:
    """
    Proxy heuristic for explicit content:
    speechness > 0.66 AND energy > 0.80 suggests rap/explicit hip-hop.
    """
    return (
        float(track.get("speechness", 0)) > 0.66
        and float(track.get("energy", 0)) > 0.80
    )


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
    exclude_artists  = _normalise_list(inputs.get("exclude_artists", ""))
    exclude_genres   = _normalise_list(inputs.get("exclude_genres", ""))
    filter_explicit  = inputs.get("filter_explicit", True)
    song_type_filter = (inputs.get("song_type_filter") or "").strip().lower()
    _LABEL_ARTISTS = {"AMU": "amu", "FPO": "fpo"}
    label_filter = [_LABEL_ARTISTS[c.upper()] for c in (inputs.get("labels") or []) if c.upper() in _LABEL_ARTISTS]

    tempo_min  = day_part.get("tempo_min",  60)
    tempo_max  = day_part.get("tempo_max", 180)
    energy_min = max(0.0, day_part.get("energy_min",  0.0) - relax)
    energy_max = min(1.0, day_part.get("energy_max",  1.0) + relax)
    valence_min = max(0.0, day_part.get("valence_min", 0.0) - relax)
    valence_max = min(1.0, day_part.get("valence_max", 1.0) + relax)

    filtered = []
    for track in candidates:
        if _is_banned(track):
            continue
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
        if song_type_filter:
            track_type = (track.get("song_type") or "").strip().lower()
            if track_type and track_type != song_type_filter:
                continue
        if label_filter:
            artist = (track.get("artist") or "").lower()
            if not any(lbl in artist for lbl in label_filter):
                continue

        filtered.append(track)

    return filtered


def _apply_exclusion_filters_only(
    candidates: list[dict],
    inputs:     dict,
) -> list[dict]:
    """
    Apply hard filters: genre exclusions, genre inclusions (when specified),
    artist exclusions, explicit proxy, song type, and label filters.
    """
    exclude_artists  = _normalise_list(inputs.get("exclude_artists", ""))
    exclude_genres   = _normalise_list(inputs.get("exclude_genres", ""))
    include_genres   = _normalise_list(inputs.get("include_genres", ""))
    filter_explicit  = inputs.get("filter_explicit", True)
    song_type_filter = (inputs.get("song_type_filter") or "").strip().lower()
    _LABEL_ARTISTS = {"AMU": "amu", "FPO": "fpo"}
    label_filter = [_LABEL_ARTISTS[c.upper()] for c in (inputs.get("labels") or []) if c.upper() in _LABEL_ARTISTS]

    filtered = []
    for track in candidates:
        if _is_banned(track):
            continue
        artist = (track.get("artist") or "").lower()
        genre  = _norm_genre(track.get("genre"))
        if any(ea.lower() in artist for ea in exclude_artists if ea):
            continue
        if any(_norm_genre(eg) in genre for eg in exclude_genres if eg):
            continue
        # Include genres: if any are specified, only allow tracks matching at least one
        if include_genres and not any(_norm_genre(ig) in genre for ig in include_genres if ig):
            continue
        if filter_explicit and _is_explicit_proxy(track):
            continue
        if song_type_filter and song_type_filter != 'mixed':
            track_type = (track.get("song_type") or "").strip().lower()
            if track_type and track_type != song_type_filter:
                continue
        if label_filter:
            if not any(lbl in artist for lbl in label_filter):
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


# ─── Main retriever ───────────────────────────────────────────────────────────

# Top N songs passed to MMR per day-part (sorted by relevance).
# 5 day-parts × 75 tracks = 375 max needed; 800 gives comfortable headroom.
MAX_CANDIDATES = 800


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

    if not all_playable:
        return [], [], {"filtered_count": 0, "skipped": True}

    # 3. Sort by relevance to this day-part, with a genre-match boost so tracks
    #    whose genre aligns with the day-part's genre_emphasis float to the top.
    #    Put fresh (unused) tracks first so the MAX_CANDIDATES window is dominated
    #    by songs not yet in earlier day-parts.
    _genre_emphasis = {_norm_genre(g) for g in (day_part_params.get("genre_emphasis") or [])}

    def _sort_key(t: dict) -> float:
        rel = compute_relevance(t, day_part_params)
        if _genre_emphasis and _norm_genre(t.get("genre", "")) in _genre_emphasis:
            rel *= 1.15
        return rel

    all_playable.sort(key=_sort_key, reverse=True)
    if used_song_ids:
        fresh = [t for t in all_playable if t.get("song_id") not in used_song_ids]
        stale = [t for t in all_playable if t.get("song_id") in used_song_ids]
        candidates = (fresh + stale)[:MAX_CANDIDATES]
    else:
        candidates = all_playable[:MAX_CANDIDATES]

    return candidates, all_playable, {"filtered_count": len(candidates), "skipped": False}


def fetch_must_include_tracks(
    must_include_artists: list[str],
    candidates:           list[dict],
    target_count:         int,
    day_part_params:      dict | None = None,
) -> list[dict]:
    """
    Pre-select tracks for must-include artists.
    - First looks in candidates pool.
    - Falls back to direct SQL query if artist not found.
    - Capped at 15% of target playlist length.
    - When day_part_params is given, each artist's tracks are ranked by
      relevance to the day-part targets so the best-fitting ones are injected.

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

        # Take best 1–2 tracks per must-include artist. No relevance floor here:
        # the artist was explicitly requested, so we honour it with their
        # closest-fitting tracks even when the fit is imperfect.
        if day_part_params:
            matches.sort(key=lambda t: compute_relevance(t, day_part_params), reverse=True)
        selected.extend(matches[:2])
        if len(selected) >= cap:
            break

    return selected[:cap]


# Injected genre tracks must fit the day-part at least this well (BFS floor).
# Body tracks picked by MMR typically score ~0.8; below this floor a genre
# match is a mood mismatch (e.g. a max-energy track in a gentle morning slot)
# and gets skipped rather than pinned to the head of the playlist.
MIN_GENRE_INJECT_RELEVANCE = 0.65


def fetch_must_include_genre_tracks(
    include_genres:  list[str],
    candidates:      list[dict],
    target_count:    int,
    day_part_params: dict | None = None,
) -> list[dict]:
    """
    Pre-select tracks for must-include genres.
    - First looks in candidates pool.
    - Falls back to direct SQL query if genre not found.
    - Capped at 30% of target playlist length.
    - When day_part_params is given, matches are ranked by relevance to the
      day-part targets (the BFS score) and tracks below
      MIN_GENRE_INJECT_RELEVANCE are dropped, so genre injection can no longer
      pin poorly-fitting songs to the head of every day-part.

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

        # Rank by fit to the day-part targets and drop mood mismatches, so the
        # injected tracks obey the soundboard settings like every other track.
        if day_part_params:
            scored = [(compute_relevance(t, day_part_params), t) for t in matches]
            scored.sort(key=lambda st: st[0], reverse=True)
            matches = [t for rel, t in scored if rel >= MIN_GENRE_INJECT_RELEVANCE]
            if not matches:
                logger.warning(
                    "Must-include genre '%s': no tracks fit the day-part targets (best relevance %.2f < %.2f). Skipping.",
                    genre_name, scored[0][0] if scored else 0.0, MIN_GENRE_INJECT_RELEVANCE,
                )
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
