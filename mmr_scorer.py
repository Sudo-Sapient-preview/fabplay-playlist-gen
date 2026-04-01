"""
mmr_scorer.py — Maximal Marginal Relevance algorithm for fabPLAY v3.0
"""

import math
import logging

logger = logging.getLogger(__name__)

WEIGHTS = {
    "energy":           0.25,
    "valence":          0.20,
    "tempo":            0.20,
    "danceability":     0.15,
    "acousticness":     0.10,
    "instrumentalness": 0.10,
}


def _norm_tempo(bpm: float) -> float:
    return (bpm - 60) / 120


def compute_relevance(track: dict, dp: dict) -> float:
    distance = math.sqrt(
        WEIGHTS["energy"]           * (float(track.get("energy",           0.5)) - float(dp.get("energy_target",           0.5))) ** 2 +
        WEIGHTS["valence"]          * (float(track.get("valence",          0.5)) - float(dp.get("valence_target",          0.6))) ** 2 +
        WEIGHTS["tempo"]            * (_norm_tempo(float(track.get("tempo_bpm", 120))) - _norm_tempo(float(dp.get("tempo_target", 110)))) ** 2 +
        WEIGHTS["danceability"]     * (float(track.get("danceability",     0.5)) - float(dp.get("danceability_target",     0.5))) ** 2 +
        WEIGHTS["acousticness"]     * (float(track.get("acousticness",     0.4)) - float(dp.get("acousticness_target",     0.4))) ** 2 +
        WEIGHTS["instrumentalness"] * (float(track.get("instrumentalness", 0.3)) - float(dp.get("instrumentalness_target", 0.3))) ** 2
    )
    return max(0.0, 1.0 - distance)


def compute_track_sim(track_a: dict, track_b: dict) -> float:
    distance = math.sqrt(
        WEIGHTS["energy"]           * (float(track_a.get("energy",           0.5)) - float(track_b.get("energy",           0.5))) ** 2 +
        WEIGHTS["valence"]          * (float(track_a.get("valence",          0.5)) - float(track_b.get("valence",          0.5))) ** 2 +
        WEIGHTS["tempo"]            * (_norm_tempo(float(track_a.get("tempo_bpm", 120))) - _norm_tempo(float(track_b.get("tempo_bpm", 120)))) ** 2 +
        WEIGHTS["danceability"]     * (float(track_a.get("danceability",     0.5)) - float(track_b.get("danceability",     0.5))) ** 2 +
        WEIGHTS["acousticness"]     * (float(track_a.get("acousticness",     0.4)) - float(track_b.get("acousticness",     0.4))) ** 2 +
        WEIGHTS["instrumentalness"] * (float(track_a.get("instrumentalness", 0.3)) - float(track_b.get("instrumentalness", 0.3))) ** 2
    )
    return max(0.0, 1.0 - distance)


def compute_mmr_score(candidate: dict, dp: dict, selected: list[dict], lam: float) -> float:
    relevance = compute_relevance(candidate, dp)
    if not selected:
        return relevance
    max_sim = max(compute_track_sim(candidate, s) for s in selected)
    return lam * relevance - (1.0 - lam) * max_sim


def _fill_to_duration(ordered: list[dict], target_seconds: float, target_count: int) -> list[dict]:
    if not ordered:
        return []
    playlist, total_duration = [], 0.0
    n, max_tracks = len(ordered), len(ordered) * 5
    while len(playlist) < max_tracks:
        track = ordered[len(playlist) % n]
        playlist.append(track)
        total_duration += float(track.get("duration_seconds", 210))
        if total_duration >= target_seconds and len(playlist) >= target_count:
            break
    return playlist


def mmr_select(
    candidates:     list[dict],
    must_include:   list[dict],
    dp:             dict,
    target_count:   int,
    target_seconds: float,
    lam:            float = 0.7,
) -> list[dict]:
    must_ids = {t.get("song_id") for t in must_include}
    for t in must_include:
        rel = compute_relevance(t, dp)
        t["relevance_score"] = rel
        t["mmr_score"] = rel

    selected = list(must_include)
    pool = [t for t in candidates if t.get("song_id") not in must_ids]
    total_duration = sum(float(t.get("duration_seconds", 210)) for t in selected)

    while pool and (len(selected) < target_count or total_duration < target_seconds):
        best_score, best_idx = None, 0
        for i, c in enumerate(pool):
            score = compute_mmr_score(c, dp, selected, lam)
            if best_score is None or score > best_score:
                best_score, best_idx = score, i
        chosen = pool.pop(best_idx)
        chosen["relevance_score"] = compute_relevance(chosen, dp)
        chosen["mmr_score"] = best_score if best_score is not None else 0.0
        selected.append(chosen)
        total_duration += float(chosen.get("duration_seconds", 210))

    if total_duration < target_seconds or len(selected) < target_count:
        selected = _fill_to_duration(selected, target_seconds, target_count)

    return selected
