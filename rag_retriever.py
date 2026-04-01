"""
rag_retriever.py — BFS-based retrieval pipeline for fabPLAY v2.0

For each day-part:
  1. Fetch full song catalog from DB
  2. Apply hard filters (exclusions, tempo/energy/valence bounds)
  3. Min-candidate check with filter relaxation fallback
"""

import logging

from db import fetch_all_songs, fetch_songs_by_artist, fetch_songs_by_genre

logger = logging.getLogger(__name__)

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

    tempo_min  = day_part.get("tempo_min",  60)
    tempo_max  = day_part.get("tempo_max", 180)
    energy_min = max(0.0, day_part.get("energy_min",  0.0) - relax)
    energy_max = min(1.0, day_part.get("energy_max",  1.0) + relax)
    valence_min = max(0.0, day_part.get("valence_min", 0.0) - relax)
    valence_max = min(1.0, day_part.get("valence_max", 1.0) + relax)

    filtered = []
    for track in candidates:
        artist = (track.get("artist") or "").lower()
        genre  = (track.get("genre")  or "").lower()
        tempo  = float(track.get("tempo_bpm", 120))
        energy = float(track.get("energy",    0.5))
        valence = float(track.get("valence",  0.5))

        if any(ea.lower() in artist for ea in exclude_artists if ea):
            continue
        if any(eg.lower() in genre for eg in exclude_genres if eg):
            continue
        if not (tempo_min <= tempo <= tempo_max):
            continue
        if not (energy_min <= energy <= energy_max):
            continue
        if not (valence_min <= valence <= valence_max):
            continue
        if filter_explicit and _is_explicit_proxy(track):
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

    filtered = []
    for track in candidates:
        artist = (track.get("artist") or "").lower()
        genre  = (track.get("genre")  or "").lower()
        if any(ea.lower() in artist for ea in exclude_artists if ea):
            continue
        if any(eg.lower() in genre for eg in exclude_genres if eg):
            continue
        if filter_explicit and _is_explicit_proxy(track):
            continue
        filtered.append(track)

    return filtered


def _normalise_list(raw: str | list) -> list[str]:
    if isinstance(raw, list):
        return [str(x).strip() for x in raw if str(x).strip()]
    if not raw:
        return []
    return [x.strip() for x in str(raw).split(",") if x.strip()]


# ─── Main retriever ───────────────────────────────────────────────────────────

MIN_CANDIDATES = 5


def retrieve_candidates(
    brand_profile:   dict,
    day_part_params: dict,
    inputs:          dict,
) -> tuple[list[dict], dict]:
    """
    BFS-based retrieval for one day-part.
    Fetches the full song catalog and applies hard filters.

    Returns:
        (filtered_candidates, stats_dict)
        stats_dict: {'filtered_count': int, 'skipped': bool}
    """
    dp_name = day_part_params.get("name", "")

    # 1. Fetch full catalog
    all_songs = fetch_all_songs()
    for s in all_songs:
        if "song_id" not in s:
            s["song_id"] = s.get("id")

    # 2. Hard filters (strict)
    filtered = _apply_hard_filters(all_songs, day_part_params, inputs, relax=0.0)

    # 3. Relax bounds if too few candidates
    if len(filtered) < MIN_CANDIDATES:
        logger.warning(
            "Only %d candidates after hard filters for '%s'. Relaxing filters (±0.15)...",
            len(filtered), dp_name,
        )
        filtered = _apply_hard_filters(all_songs, day_part_params, inputs, relax=0.15)

    # 4. Drop audio-feature bounds entirely if still too few
    if len(filtered) < MIN_CANDIDATES:
        logger.warning(
            "Still only %d candidates for '%s'. Dropping audio-feature bounds, "
            "keeping exclusions only.",
            len(filtered), dp_name,
        )
        filtered = _apply_exclusion_filters_only(all_songs, inputs)

    if not filtered:
        return [], {"filtered_count": 0, "skipped": True}

    return filtered, {"filtered_count": len(filtered), "skipped": False}


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

        if not matches:
            # SQL fallback
            sql_tracks = fetch_songs_by_artist(artist_name)
            if not sql_tracks:
                logger.warning("Must-include artist '%s' not found anywhere. Skipping.", artist_name)
                continue
            for t in sql_tracks:
                t["similarity"] = 0.0   # no ANN similarity for SQL-fetched tracks
                t["song_id"]    = t.get("song_id", t.get("id"))
            matches = sql_tracks

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
            if genre_name.lower() in (t.get("genre") or "").lower()
            and t.get("song_id") not in seen_ids
        ]

        if not matches:
            # SQL fallback
            sql_tracks = fetch_songs_by_genre(genre_name)
            if not sql_tracks:
                logger.warning("Must-include genre '%s' not found anywhere. Skipping.", genre_name)
                continue
            for t in sql_tracks:
                t["similarity"] = 0.0
                t["song_id"] = t.get("song_id", t.get("id"))
            matches = [t for t in sql_tracks if t.get("song_id") not in seen_ids]

        # Take up to 4 tracks per genre
        for t in matches[:4]:
            selected.append(t)
            seen_ids.add(t.get("song_id"))

        if len(selected) >= cap:
            break

    return selected[:cap]
