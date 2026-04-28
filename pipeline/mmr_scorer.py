"""
mmr_scorer.py — Maximal Marginal Relevance algorithm for fabPLAY v3.0

Performance strategy
--------------------
* All feature vectors are stacked into a numpy matrix once per day-part.
* Relevance scores are computed with a single matrix subtraction + norm.
* max_sim is maintained as a numpy array; after each pick the update is a
  single vectorised norm over the remaining rows — O(n) not O(n*k).
* Total complexity: O(n*k) float ops, all numpy-accelerated.
"""

import math
import logging
import numpy as np

logger = logging.getLogger(__name__)

# Weights applied to each feature dimension.
_W = np.array([0.25, 0.20, 0.20, 0.15, 0.10, 0.10], dtype=np.float32)
_SW = np.sqrt(_W)   # pre-sqrt so weighted L2 = ||sqrt(W) * (a-b)||


def _norm_tempo(bpm: float) -> float:
    return (bpm - 60.0) / 120.0


def _track_vec(t: dict) -> np.ndarray:
    """Extract the 6-dim feature vector for a track (raw, before sqrt-weight)."""
    return np.array([
        float(t.get("energy",           0.5)),
        float(t.get("valence",          0.5)),
        _norm_tempo(float(t.get("tempo_bpm", 120))),
        float(t.get("danceability",     0.5)),
        float(t.get("acousticness",     0.4)),
        float(t.get("instrumentalness", 0.3)),
    ], dtype=np.float32)


def _dp_vec(dp: dict) -> np.ndarray:
    return np.array([
        float(dp.get("energy_target",           0.5)),
        float(dp.get("valence_target",          0.6)),
        _norm_tempo(float(dp.get("tempo_target", 110))),
        float(dp.get("danceability_target",     0.5)),
        float(dp.get("acousticness_target",     0.4)),
        float(dp.get("instrumentalness_target", 0.3)),
    ], dtype=np.float32)


# ─── Public scalar helpers (used by retriever for candidate sorting) ──────────

def compute_relevance(track: dict, dp: dict) -> float:
    diff = (_track_vec(track) - _dp_vec(dp)) * _SW
    return float(max(0.0, 1.0 - math.sqrt(float(np.dot(diff, diff)))))


def compute_track_sim(track_a: dict, track_b: dict) -> float:
    diff = (_track_vec(track_a) - _track_vec(track_b)) * _SW
    return float(max(0.0, 1.0 - math.sqrt(float(np.dot(diff, diff)))))


def compute_mmr_score(candidate: dict, dp: dict, selected: list[dict], lam: float) -> float:
    rel = compute_relevance(candidate, dp)
    if not selected:
        return rel
    max_sim = max(compute_track_sim(candidate, s) for s in selected)
    return lam * rel - (1.0 - lam) * max_sim


# ─── Duration fill ─────────────────────────────────────────────────────────────

def _fill_to_duration(ordered: list[dict], target_seconds: float, target_count: int) -> list[dict]:
    if not ordered:
        return ordered
    total = sum(float(t.get("duration_seconds", 210)) for t in ordered)
    if total >= target_seconds and len(ordered) >= target_count:
        return ordered
    recycle = sorted(ordered, key=lambda t: t.get("relevance_score", 0.0), reverse=True)
    result, idx = list(ordered), 0
    while total < target_seconds and len(result) < target_count * 2:
        t = dict(recycle[idx % len(recycle)])
        result.append(t)
        total += float(t.get("duration_seconds", 210))
        idx += 1
    return result


# ─── Fast MMR ─────────────────────────────────────────────────────────────────

def mmr_select(
    candidates:     list[dict],
    must_include:   list[dict],
    dp:             dict,
    target_count:   int,
    target_seconds: float,
    lam:            float = 0.7,
) -> list[dict]:
    """
    Numpy-vectorised MMR with incremental max_sim update.
    Complexity: O(n*k) numpy ops vs the naive O(n*k²) Python loop.
    """
    must_ids = {t.get("song_id") for t in must_include}
    dv = _dp_vec(dp) * _SW          # weighted dp target vector

    # ── Seed selected set with must-include tracks ─────────────────────────
    for t in must_include:
        tv = _track_vec(t) * _SW
        diff = tv - dv
        rel = float(max(0.0, 1.0 - math.sqrt(float(np.dot(diff, diff)))))
        t["relevance_score"] = rel
        t["mmr_score"] = rel
    selected = list(must_include)

    pool_tracks = [t for t in candidates if t.get("song_id") not in must_ids]
    if not pool_tracks:
        return selected

    # ── Build numpy matrix for pool (n × 6), weighted ─────────────────────
    n = len(pool_tracks)
    M = np.stack([_track_vec(t) for t in pool_tracks]) * _SW   # (n, 6)

    # Relevance: distance of each row to dp target — computed once
    diff_dp = M - dv                                             # (n, 6)
    rel_scores = np.maximum(0.0, 1.0 - np.sqrt((diff_dp ** 2).sum(axis=1)))  # (n,)

    # Initialise max_sim from must-include set
    if selected:
        sel_mat = np.stack([_track_vec(t) for t in selected]) * _SW  # (k0, 6)
        # dist from each pool track to each selected track → max sim per pool track
        sims = np.maximum(0.0, 1.0 - np.sqrt(
            ((M[:, None, :] - sel_mat[None, :, :]) ** 2).sum(axis=2)
        ))  # (n, k0)
        max_sim = sims.max(axis=1)  # (n,)
    else:
        max_sim = np.zeros(n, dtype=np.float32)

    total_duration = sum(float(t.get("duration_seconds", 210)) for t in selected)

    # Keep a boolean mask so we don't pay index-rebuild cost each iteration
    active = np.ones(n, dtype=bool)
    active_indices = list(range(n))   # maps active slot → original pool index

    while active.any() and (len(selected) < target_count or total_duration < target_seconds):
        scores = lam * rel_scores - (1.0 - lam) * max_sim
        scores[~active] = -np.inf

        best_i = int(np.argmax(scores))
        best_score = float(scores[best_i])

        chosen = pool_tracks[best_i]
        chosen["relevance_score"] = float(rel_scores[best_i])
        chosen["mmr_score"] = best_score
        selected.append(chosen)
        total_duration += float(chosen.get("duration_seconds", 210))
        active[best_i] = False

        # Incremental update: sim of each remaining candidate to the new pick
        chosen_vec = M[best_i]                                         # (6,)
        new_sims = np.maximum(0.0, 1.0 - np.sqrt(
            ((M - chosen_vec) ** 2).sum(axis=1)
        ))                                                              # (n,)
        max_sim = np.maximum(max_sim, new_sims)

    if total_duration < target_seconds or len(selected) < target_count:
        selected = _fill_to_duration(selected, target_seconds, target_count)

    return selected
