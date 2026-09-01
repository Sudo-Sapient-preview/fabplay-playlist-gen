"""
embed_utils.py — Shared embedding utilities for fabPLAY v2.0

Provides:
  - Numeric-to-label mappings for all audio features
  - build_track_embed_text(song)   → used by embed_catalog.py
  - build_query_embed_text(...)    → used by rag_retriever.py

CRITICAL: Both functions must use the SAME label mappings.
Mixing label formats between track and query embeddings breaks cosine similarity.
"""


# ─── Label lookup tables ─────────────────────────────────────────────────────

def get_energy_label(energy: float) -> str:
    if energy < 0.30:
        return "calm, subdued"
    elif energy < 0.60:
        return "moderate, steady"
    elif energy < 0.80:
        return "energetic, lively"
    else:
        return "intense, high-energy"


def get_danceability_label(danceability: float) -> str:
    if danceability < 0.40:
        return "free-flowing, non-rhythmic"
    elif danceability < 0.60:
        return "moderately groovy"
    elif danceability < 0.80:
        return "groovy, danceable"
    else:
        return "highly danceable"


def get_acousticness_label(acousticness: float) -> str:
    if acousticness < 0.30:
        return "electronic, produced"
    elif acousticness < 0.60:
        return "mixed, semi-acoustic"
    else:
        return "acoustic, organic"


def get_instrumentalness_label(instrumentalness: float) -> str:
    if instrumentalness < 0.30:
        return "vocal-driven"
    elif instrumentalness < 0.70:
        return "mixed vocal/instrumental"
    else:
        return "instrumental"


def get_valence_label(valence: float) -> str:
    if valence < 0.30:
        return "melancholic, dark"
    elif valence < 0.60:
        return "neutral, bittersweet"
    elif valence < 0.80:
        return "upbeat, positive"
    else:
        return "joyful, euphoric"


def get_tempo_label(tempo_bpm: float) -> str:
    if tempo_bpm < 80:
        return "slow"
    elif tempo_bpm < 100:
        return "relaxed"
    elif tempo_bpm < 120:
        return "moderate"
    elif tempo_bpm < 140:
        return "upbeat"
    else:
        return "fast, energetic"


def get_mood_description(valence: float, energy: float) -> str:
    """Derive mood from valence × energy combination."""
    high_valence = valence >= 0.60
    high_energy  = energy  >= 0.60
    near_mid     = (0.40 <= valence < 0.60) and (0.40 <= energy < 0.60)

    if near_mid:
        return "balanced, versatile, easy-listening"
    elif high_valence and high_energy:
        return "euphoric, celebratory, feel-good"
    elif high_valence and not high_energy:
        return "warm, content, peaceful"
    elif not high_valence and high_energy:
        return "intense, driven, dark-energetic"
    else:
        return "melancholic, introspective, somber"


# ─── Track embed text ─────────────────────────────────────────────────────────

def build_track_embed_text(song: dict) -> str:
    """
    Convert a catalog row (from the catalog_songs view) into a structured
    natural-language string for embedding.

    Used by: embed_catalog.py (catalog embedding, one-time setup)
    """
    title            = song.get("title", "Unknown")
    library          = song.get("library", "Unknown")
    genre            = song.get("genre", "Unknown")
    energy           = float(song.get("energy", 0.5))
    tempo_bpm        = float(song.get("tempo_bpm", 120))
    valence          = float(song.get("valence", 0.5))
    danceability     = float(song.get("danceability", 0.5))
    acousticness     = float(song.get("acousticness", 0.5))
    instrumentalness = float(song.get("instrumentalness", 0.3))
    loudness         = float(song.get("loudness", -8.0))
    mode             = song.get("mode", "major")
    key              = song.get("key", "C")

    return (
        f"Track: {title} ({library})\n"
        f"Genre: {genre}\n"
        f"Energy: {get_energy_label(energy)} ({energy:.2f})\n"
        f"Tempo: {tempo_bpm:.0f} BPM — {get_tempo_label(tempo_bpm)}\n"
        f"Mood: {get_mood_description(valence, energy)}\n"
        f"Danceability: {get_danceability_label(danceability)} ({danceability:.2f})\n"
        f"Acousticness: {get_acousticness_label(acousticness)} ({acousticness:.2f})\n"
        f"Instrumentalness: {get_instrumentalness_label(instrumentalness)} ({instrumentalness:.2f})\n"
        f"Valence: {get_valence_label(valence)} ({valence:.2f})\n"
        f"Loudness: {loudness:.1f} dB\n"
        f"Mode: {mode}\n"
        f"Key: {key}"
    )


# ─── Query embed text ─────────────────────────────────────────────────────────

def build_query_embed_text(
    brand_profile: dict,
    day_part_params: dict,
    day_part_template: dict,
) -> str:
    """
    Construct a query embed text from brand context + day-part audio targets.
    This text is embedded to produce the query vector for pgvector ANN search.

    Uses IDENTICAL label mappings as build_track_embed_text() — mandatory for
    cosine similarity to be meaningful.

    Used by: rag_retriever.py (query embedding, per day-part per run)

    Args:
        brand_profile:    Parsed JSON from OpenRouter Call 1
        day_part_params:  One day-part dict from the Sound Board JSON (Call 2)
        day_part_template: Matching day-part template from day_part_templates.py
    """
    brand_name           = brand_profile.get("brand_name", "")
    brand_summary        = brand_profile.get("brand_summary", "")
    visual_aesthetic     = brand_profile.get("visual_aesthetic", "")
    customer_description = brand_profile.get("customer_description", "")
    price_positioning    = brand_profile.get("price_positioning", "")
    brand_adjectives     = brand_profile.get("brand_adjectives", [])
    if isinstance(brand_adjectives, list):
        brand_adjectives_str = ", ".join(brand_adjectives)
    else:
        brand_adjectives_str = str(brand_adjectives)

    dp_name         = day_part_params.get("name", day_part_template.get("name", ""))
    start_time      = day_part_params.get("start_time", day_part_template.get("start_time", ""))
    end_time        = day_part_params.get("end_time", day_part_template.get("end_time", ""))
    character_desc  = day_part_params.get("character_description", day_part_template.get("character", ""))
    genre_emphasis  = day_part_params.get("genre_emphasis", [])
    if isinstance(genre_emphasis, list):
        genre_emphasis_str = ", ".join(genre_emphasis)
    else:
        genre_emphasis_str = str(genre_emphasis)

    energy_target        = float(day_part_params.get("energy_target", 0.5))
    tempo_target         = float(day_part_params.get("tempo_target", 110))
    valence_target       = float(day_part_params.get("valence_target", 0.6))
    danceability_target  = float(day_part_params.get("danceability_target", 0.5))
    acousticness_target  = float(day_part_params.get("acousticness_target", 0.4))
    instrumentalness_target = float(day_part_params.get("instrumentalness_target", 0.35))

    return (
        f"Music for {brand_name} — {dp_name} ({start_time}–{end_time})\n"
        f"Brand: {brand_summary}\n"
        f"Aesthetic: {visual_aesthetic}\n"
        f"Customer: {customer_description}\n"
        f"Segment: {price_positioning}\n"
        f"Brand adjectives: {brand_adjectives_str}\n"
        f"Day-part character: {character_desc}\n"
        f"Genres: {genre_emphasis_str}\n"
        f"\n"
        f"Audio targets:\n"
        f"Energy: {get_energy_label(energy_target)} ({energy_target:.2f})\n"
        f"Tempo: {tempo_target:.0f} BPM — {get_tempo_label(tempo_target)}\n"
        f"Mood: {get_valence_label(valence_target)} ({valence_target:.2f})\n"
        f"Danceability: {get_danceability_label(danceability_target)} ({danceability_target:.2f})\n"
        f"Acousticness: {get_acousticness_label(acousticness_target)} ({acousticness_target:.2f})\n"
        f"Instrumentalness: {get_instrumentalness_label(instrumentalness_target)} ({instrumentalness_target:.2f})"
    )
