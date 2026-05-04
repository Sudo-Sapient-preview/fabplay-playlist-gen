"""
mmr_scorer.py — Maximal Marginal Relevance algorithm for fabPLAY v3.0

Performance strategy
--------------------
* Feature vectors (n × 6) and CLAP embedding matrix (n × 512) stacked once.
* Relevance uses compute_relevance(): weighted L2 + arousal + mood boosts.
* Diversity uses CLAP cosine when embeddings are present, L2 fallback otherwise.
* max_sim maintained as a numpy array with O(n) incremental updates per pick.
* Total complexity: O(n*k) numpy ops, all vectorised.
"""

import math
import logging
import numpy as np

logger = logging.getLogger(__name__)

# Weights applied to each feature dimension.
_W = np.array([0.25, 0.20, 0.20, 0.15, 0.10, 0.10], dtype=np.float32)
_SW = np.sqrt(_W)   # pre-sqrt so weighted L2 = ||sqrt(W) * (a-b)||

# Data-driven label scores derived from avg arousal/valence across all 18,513
# songs in analysis_song_features. Covers all 56 distinct labels in the catalog.
# Format: label → (avg_normalised_arousal 0-1, avg_normalised_valence 0-1)
_LABEL_SCORES: dict[str, tuple[float, float]] = {
    "action":       (0.558, 0.597),
    "adventure":    (0.523, 0.647),
    "advertising":  (0.500, 0.656),
    "background":   (0.482, 0.641),
    "ballad":       (0.439, 0.581),
    "calm":         (0.394, 0.572),
    "children":     (0.471, 0.659),
    "christmas":    (0.461, 0.616),
    "commercial":   (0.559, 0.684),
    "cool":         (0.501, 0.637),
    "corporate":    (0.535, 0.679),
    "dark":         (0.495, 0.567),
    "deep":         (0.569, 0.665),
    "documentary":  (0.447, 0.608),
    "drama":        (0.420, 0.558),
    "dramatic":     (0.433, 0.530),
    "dream":        (0.441, 0.580),
    "emotional":    (0.371, 0.526),
    "energetic":    (0.653, 0.695),
    "epic":         (0.453, 0.501),
    "fast":         (0.675, 0.709),
    "film":         (0.441, 0.572),
    "fun":          (0.626, 0.703),
    "funny":        (0.611, 0.703),
    "game":         (0.531, 0.630),
    "groovy":       (0.577, 0.675),
    "happy":        (0.597, 0.700),
    "heavy":        (0.659, 0.607),
    "holiday":      (0.490, 0.628),
    "hopeful":      (0.445, 0.599),
    "inspiring":    (0.477, 0.640),
    "love":         (0.528, 0.622),
    "meditative":   (0.304, 0.483),
    "melancholic":  (0.394, 0.540),
    "melodic":      (0.566, 0.663),
    "motivational": (0.519, 0.658),
    "movie":        (0.463, 0.580),
    "nature":       (0.374, 0.563),
    "party":        (0.655, 0.713),
    "positive":     (0.588, 0.725),
    "powerful":     (0.682, 0.648),
    "relaxing":     (0.330, 0.527),
    "retro":        (0.647, 0.700),
    "romantic":     (0.430, 0.598),
    "sad":          (0.400, 0.545),
    "sexy":         (0.586, 0.673),
    "slow":         (0.364, 0.514),
    "soft":         (0.407, 0.585),
    "soundscape":   (0.334, 0.470),
    "space":        (0.485, 0.566),
    "sport":        (0.653, 0.711),
    "summer":       (0.583, 0.689),
    "trailer":      (0.454, 0.527),
    "travel":       (0.494, 0.635),
    "upbeat":       (0.646, 0.719),
    "uplifting":    (0.593, 0.699),
}


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


def _mood_alignment(labels: set[str], dp: dict) -> float:
    """0-1 score: how well mood labels match day-part energy/valence targets.

    Uses continuous avg arousal/valence scores derived from the full catalog
    so every label contributes, not just the 40 hardcoded words.
    """
    energy_target  = float(dp.get("energy_target",  0.5))
    valence_target = float(dp.get("valence_target", 0.5))

    arousal_vals = [_LABEL_SCORES[l][0] for l in labels if l in _LABEL_SCORES]
    valence_vals = [_LABEL_SCORES[l][1] for l in labels if l in _LABEL_SCORES]

    scores = []
    if arousal_vals:
        scores.append(1.0 - abs(sum(arousal_vals) / len(arousal_vals) - energy_target))
    if valence_vals:
        scores.append(1.0 - abs(sum(valence_vals) / len(valence_vals) - valence_target))

    return sum(scores) / len(scores) if scores else 0.5


def _cosine_similarity(a: list, b: list) -> float:
    dot   = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


# ─── Public scalar helpers (used by retriever for candidate sorting) ──────────

def compute_relevance(track: dict, dp: dict) -> float:
    scalar = float(max(0.0, 1.0 - math.sqrt(float(np.dot(
        (_track_vec(track) - _dp_vec(dp)) * _SW,
        (_track_vec(track) - _dp_vec(dp)) * _SW,
    )))))
    boosts = []

    arousal = track.get("arousal")
    if arousal is not None:
        norm_arousal = max(0.0, min(1.0, (float(arousal) - 1.0) / 8.0))
        boosts.append(1.0 - abs(norm_arousal - float(dp.get("energy_target", 0.5))))

    labels = {m.lower() for m in (track.get("mood_predicted_labels") or [])}
    if labels:
        boosts.append(_mood_alignment(labels, dp))

    if not boosts:
        return scalar
    return 0.5 * scalar + 0.5 * (sum(boosts) / len(boosts))


def compute_track_sim(track_a: dict, track_b: dict) -> float:
    emb_a = track_a.get("clap_audio_512")
    emb_b = track_b.get("clap_audio_512")
    if emb_a and emb_b:
        return _cosine_similarity(emb_a, emb_b)
    diff = (_track_vec(track_a) - _track_vec(track_b)) * _SW
    return float(max(0.0, 1.0 - math.sqrt(float(np.dot(diff, diff)))))


def compute_mmr_score(candidate: dict, dp: dict, selected: list[dict], lam: float) -> float:
    rel = compute_relevance(candidate, dp)
    if not selected:
        return rel
    max_sim = max(compute_track_sim(candidate, s) for s in selected)
    return lam * rel - (1.0 - lam) * max_sim



# ─── Fast MMR ─────────────────────────────────────────────────────────────────

_EMB_DIM = 512


def _build_emb_matrix(tracks: list[dict]) -> tuple[np.ndarray | None, np.ndarray]:
    """
    Build a unit-normalised (n × 512) CLAP embedding matrix and a boolean mask.
    Returns (E_norm, has_emb). E_norm is None when no track has an embedding.
    """
    n = len(tracks)
    has_emb = np.array(
        [bool(t.get("clap_audio_512") and len(t["clap_audio_512"]) == _EMB_DIM) for t in tracks],
        dtype=bool,
    )
    if not has_emb.any():
        return None, has_emb
    E = np.zeros((n, _EMB_DIM), dtype=np.float32)
    for i, t in enumerate(tracks):
        if has_emb[i]:
            E[i] = t["clap_audio_512"]
    norms = np.linalg.norm(E, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return E / norms, has_emb


def _emb_sim(E_norm: np.ndarray, idx: int) -> np.ndarray:
    """Cosine similarity of all rows in E_norm to row idx, mapped to [0, 1]."""
    return (E_norm @ E_norm[idx] + 1.0) / 2.0


def mmr_select(
    candidates:     list[dict],
    must_include:   list[dict],
    dp:             dict,
    target_count:   int,
    target_seconds: float,
    lam:            float = 0.7,
    spillover:      list[dict] | None = None,
) -> list[dict]:
    """
    Numpy-vectorised MMR with incremental max_sim update.

    Diversity uses CLAP 512-dim cosine similarity when embeddings are available,
    falling back to weighted L2 on audio features otherwise.
    Relevance uses compute_relevance() so arousal and mood labels are included.
    """
    must_ids = {t.get("song_id") for t in must_include}

    # Gap 3: must-include tracks scored with full relevance (arousal + mood)
    for t in must_include:
        rel = compute_relevance(t, dp)
        t["relevance_score"] = rel
        t["mmr_score"] = rel
    selected = list(must_include)

    pool_tracks = [t for t in candidates if t.get("song_id") not in must_ids]
    if not pool_tracks:
        return selected

    n = len(pool_tracks)

    # Feature matrix (n × 6) — used as fallback when embeddings are absent
    dv = _dp_vec(dp) * _SW
    M = np.stack([_track_vec(t) for t in pool_tracks]) * _SW   # (n, 6)

    # Gap 2: relevance includes arousal + mood boosts, not just feature distance
    rel_scores = np.array([compute_relevance(t, dp) for t in pool_tracks], dtype=np.float32)

    # Gap 1: CLAP embedding matrix for cosine-based diversity
    E_norm, has_emb = _build_emb_matrix(pool_tracks)

    # ── Initialise max_sim from must-include set ───────────────────────────
    if selected:
        sel_mat = np.stack([_track_vec(t) for t in selected]) * _SW  # (k0, 6)
        sims = np.maximum(0.0, 1.0 - np.sqrt(
            ((M[:, None, :] - sel_mat[None, :, :]) ** 2).sum(axis=2)
        ))  # (n, k0)
        max_sim = sims.max(axis=1)  # (n,)
        # Override with cosine where both pool track and selected track have embeddings
        if E_norm is not None:
            for sel_track in selected:
                sel_emb = sel_track.get("clap_audio_512")
                if sel_emb and len(sel_emb) == _EMB_DIM:
                    sv = np.array(sel_emb, dtype=np.float32)
                    sv /= (np.linalg.norm(sv) or 1.0)
                    cos = (E_norm @ sv + 1.0) / 2.0          # (n,) in [0, 1]
                    max_sim = np.where(has_emb, np.maximum(max_sim, cos), max_sim)
    else:
        max_sim = np.zeros(n, dtype=np.float32)

    total_duration = sum(float(t.get("duration_seconds", 210)) for t in selected)
    active = np.ones(n, dtype=bool)

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

        # Incremental max_sim update: cosine when chosen has embedding, else L2
        chosen_vec = M[best_i]
        feat_sims = np.maximum(0.0, 1.0 - np.sqrt(((M - chosen_vec) ** 2).sum(axis=1)))
        if E_norm is not None and has_emb[best_i]:
            cos_sims = _emb_sim(E_norm, best_i)
            new_sims = np.where(has_emb, cos_sims, feat_sims)
        else:
            new_sims = feat_sims
        max_sim = np.maximum(max_sim, new_sims)

    # Fill remaining duration from spillover pool if needed
    if total_duration < target_seconds and spillover:
        selected_ids = {t.get("song_id") for t in selected}
        fill_pool = [t for t in spillover if t.get("song_id") not in selected_ids]
        fill_pool.sort(key=lambda t: compute_relevance(t, dp), reverse=True)
        for t in fill_pool:
            if total_duration >= target_seconds:
                break
            t = dict(t)
            t["relevance_score"] = compute_relevance(t, dp)
            t["mmr_score"] = t["relevance_score"]
            selected.append(t)
            total_duration += float(t.get("duration_seconds", 210))

    return selected
