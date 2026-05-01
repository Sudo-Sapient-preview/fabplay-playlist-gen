"""
sound_board.py — Azure OpenAI Call 2: Brand Profile + day-part templates → Sound Board JSON

Produces per-day-part audio targets (energy, valence, tempo, danceability,
acousticness, instrumentalness, genre_emphasis, character_description).
"""

import json
import logging
import time

from openai import AzureOpenAI, RateLimitError, AuthenticationError, APIConnectionError

from brand_pipeline.day_part_templates import DAY_PART_TEMPLATES

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are a music supervisor specializing in retail soundscaping.
Map brand personality scores to specific Spotify-style audio feature targets per day-part.
Return ONLY a valid JSON object. No markdown, no explanation, raw JSON only.

Use the Aaker-to-Audio mapping below to inform your targets:

| Brand Dimension  | High Score → Music Signal                                             | Low Score → Music Signal                          |
|------------------|-----------------------------------------------------------------------|---------------------------------------------------|
| Sincerity        | Higher acousticness, lower energy, folk/acoustic, major mode          | Lower acousticness, electronic/synthetic           |
| Excitement       | High energy, high danceability, high tempo, pop/electronic            | Low energy, slow tempo, ambient or classical       |
| Competence       | Moderate energy, structured, jazz/orchestral, consistent tempo        | Erratic energy, unconventional structures          |
| Sophistication   | High instrumentalness, low speechiness, classical/jazz, low loudness  | High speechiness, vocal-heavy, high loudness       |
| Ruggedness       | Higher loudness, rock/country, raw sound, lower instrumentalness      | Soft, polished, low loudness, high production     |

Customer segment baseline adjustments (apply on top of AI targets):
| Segment   | Energy  | Tempo   | Acousticness |
|-----------|---------|---------|--------------|
| value     | +0.10   | +5 BPM  | −0.05        |
| mid_range | 0       | 0       | 0            |
| premium   | −0.05   | −5 BPM  | +0.05        |
| luxury    | −0.10   | −10 BPM | +0.10        |

Required output structure:
{
  "sound_board": {
    "energy_target": float,
    "valence_target": float,
    "tempo_target": int,
    "danceability_target": float,
    "acousticness_target": float,
    "instrumentalness_target": float,
    "primary_genres": [string],
    "secondary_genres": [string]
  },
  "day_parts": [
    {
      "name": string,
      "start_time": "HH:MM",
      "end_time": "HH:MM",
      "energy_target": float,
      "energy_min": float,
      "energy_max": float,
      "valence_target": float,
      "valence_min": float,
      "valence_max": float,
      "tempo_target": int,
      "tempo_min": int,
      "tempo_max": int,
      "danceability_target": float,
      "acousticness_target": float,
      "instrumentalness_target": float,
      "genre_emphasis": [string],
      "character_description": string
    }
  ]
}

All float values must be clamped 0.0–1.0. All tempo values in BPM (int).
The day_parts array must contain exactly the day-parts listed in the template provided.
CRITICAL CONSTRAINT: For every day-part, the target MUST lie within its min/max range:
  energy_min <= energy_target <= energy_max
  valence_min <= valence_target <= valence_max
  tempo_min <= tempo_target <= tempo_max
The target should ideally be near the centre of the min/max range.
"""


def _build_user_prompt(brand_profile: dict, category: str, music_notes: str = "") -> str:
    templates = DAY_PART_TEMPLATES.get(category) or DAY_PART_TEMPLATES["cafe"]
    template_str = json.dumps(templates, indent=2)

    # Strip all semantic/text fields — only pass numeric personality scores and
    # audio baselines so genre_emphasis is derived from Aaker scores, not brand name.
    numeric_profile = {
        "sincerity":             brand_profile.get("sincerity",             0.5),
        "excitement":            brand_profile.get("excitement",            0.5),
        "competence":            brand_profile.get("competence",            0.5),
        "sophistication":        brand_profile.get("sophistication",        0.5),
        "ruggedness":            brand_profile.get("ruggedness",            0.5),
        "price_positioning":     brand_profile.get("price_positioning",     "mid_range"),
        "music_energy_baseline": brand_profile.get("music_energy_baseline", 0.5),
        "music_valence_baseline": brand_profile.get("music_valence_baseline", 0.5),
        "music_tempo_baseline":  brand_profile.get("music_tempo_baseline",  110),
    }

    notes_block = (
        f"\nAdditional Music Instructions (apply these to per-day-part targets):\n{music_notes}\n"
        if music_notes and music_notes.strip()
        else ""
    )

    return (
        f"Brand Personality Scores:\n{json.dumps(numeric_profile, indent=2)}\n\n"
        f"Business Category: {category}\n\n"
        f"Day-Part Templates for this category:\n{template_str}\n"
        f"{notes_block}\n"
        "Generate Sound Board parameters and per-day-part audio targets "
        "based solely on the personality scores above and the character of each day-part. "
        "Derive genre_emphasis from the Aaker score mappings — do not infer genres from any brand name or description."
        + (" Honour any Additional Music Instructions above when setting per-day-part targets." if notes_block else "")
    )


def get_sound_board(
    brand_profile: dict,
    category: str,
    chat_client: AzureOpenAI,
    deployment: str,
    max_retries: int = 3,
    music_notes: str = "",
) -> dict:
    """
    Call Azure OpenAI to produce a Sound Board JSON from the Brand Profile.

    Args:
        brand_profile: Parsed Brand Profile dict from Call 1.
        category:      Business category string (e.g. "cafe").
        chat_client:   Configured AzureOpenAI client.
        deployment:    Chat model deployment name.
        max_retries:   Max retry attempts on rate-limit errors.
        music_notes:   Free-text instructions from the brand form (e.g. "upbeat after 6pm").

    Returns:
        Parsed Sound Board dict with 'sound_board' and 'day_parts' keys.
    """
    user_prompt = _build_user_prompt(brand_profile, category, music_notes)

    for attempt in range(1, max_retries + 1):
        try:
            response = chat_client.chat.completions.create(
                model=deployment,
                max_completion_tokens=2000,
                response_format={"type": "json_object"},
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user",   "content": user_prompt},
                ],
            )
            raw = response.choices[0].message.content

            try:
                sound_board = json.loads(raw)
                _clamp_targets_to_ranges(sound_board)
                _validate_sound_board(sound_board, category)
                return sound_board

            except json.JSONDecodeError as e:
                logger.error("Sound Board JSON parse error (attempt %d): %s", attempt, e)
                _log_to_debug_file(raw, "sound_board_parse_error")
                if attempt == max_retries:
                    raise RuntimeError(
                        "Failed to parse Sound Board JSON after "
                        f"{max_retries} attempts. See azure_openai_debug.log."
                    ) from e
                continue

            except ValueError as e:
                logger.error("Sound Board validation error (attempt %d): %s", attempt, e)
                if attempt == max_retries:
                    raise
                continue

        except RateLimitError:
            wait = 10 * attempt
            print(f"  [Rate limit] Waiting {wait}s before retry {attempt}/{max_retries}...")
            time.sleep(wait)

        except AuthenticationError:
            print("[ERROR] Invalid Azure OpenAI credentials — check AZURE_OPENAI_API_KEY and AZURE_OPENAI_ENDPOINT in .env")
            raise

        except APIConnectionError:
            print("[ERROR] Cannot reach Azure OpenAI endpoint — check AZURE_OPENAI_ENDPOINT in .env")
            raise

    raise RuntimeError("Sound Board generation failed after all retries.")


def _clamp_targets_to_ranges(sound_board: dict) -> None:
    """
    Fix LLM hallucinations: recenter each min/max range around its target value.
    The range WIDTH (spread) from the LLM is preserved; only the center shifts
    to match the target so the slider thumb always sits in the middle of the
    pink band on the UI.
    """
    def recenter(tgt: float, mn: float, mx: float, lo: float, hi: float):
        if mn > mx:
            mn, mx = mx, mn
        half = (mx - mn) / 2.0
        new_mn = max(lo, tgt - half)
        new_mx = min(hi, tgt + half)
        return new_mn, new_mx

    for dp in sound_board.get("day_parts", []):
        for param in ["energy", "valence"]:
            mn  = dp.get(f"{param}_min")
            mx  = dp.get(f"{param}_max")
            tgt = dp.get(f"{param}_target")
            if mn is None or mx is None or tgt is None:
                continue
            new_mn, new_mx = recenter(tgt, mn, mx, 0.0, 1.0)
            dp[f"{param}_min"] = round(new_mn, 3)
            dp[f"{param}_max"] = round(new_mx, 3)
        mn  = dp.get("tempo_min")
        mx  = dp.get("tempo_max")
        tgt = dp.get("tempo_target")
        if mn is not None and mx is not None and tgt is not None:
            new_mn, new_mx = recenter(float(tgt), float(mn), float(mx), 60.0, 200.0)
            dp["tempo_min"] = int(new_mn)
            dp["tempo_max"] = int(new_mx)


def _validate_sound_board(data: dict, category: str) -> None:
    """Raise ValueError if the Sound Board JSON is missing required structure."""
    if "sound_board" not in data:
        raise ValueError("Sound Board JSON missing 'sound_board' key.")
    if "day_parts" not in data:
        raise ValueError("Sound Board JSON missing 'day_parts' key.")
    if not isinstance(data["day_parts"], list):
        raise ValueError("Sound Board 'day_parts' must be a list.")
    expected_count = len(DAY_PART_TEMPLATES.get(category, []))
    actual_count   = len(data["day_parts"])
    # If a template exists, require at least that many day-parts
    if expected_count > 0 and actual_count == 0:
        raise ValueError("Sound Board 'day_parts' must be a non-empty list.")
    # If no template exists, accept whatever the LLM produced (>0)
    if expected_count == 0 and actual_count == 0:
        raise ValueError("Sound Board returned 0 day_parts for unknown category.")
    if actual_count != expected_count and expected_count > 0:
        logger.warning(
            "Sound Board has %d day-parts but template has %d for category '%s'.",
            actual_count, expected_count, category,
        )


def apply_segment_adjustments(sound_board: dict, segment: str) -> dict:
    """
    Apply customer segment baseline adjustments to all day-part targets.
    Clamps all float values to [0.0, 1.0] and tempo to [60, 200].
    """
    adjustments = {
        "value":     {"energy": +0.10, "tempo": +5,  "acousticness": -0.05},
        "mid_range": {"energy":  0.00, "tempo":  0,  "acousticness":  0.00},
        "premium":   {"energy": -0.05, "tempo": -5,  "acousticness": +0.05},
        "luxury":    {"energy": -0.10, "tempo": -10, "acousticness": +0.10},
    }
    adj = adjustments.get(segment, adjustments["mid_range"])

    def clamp(v: float, lo: float = 0.0, hi: float = 1.0) -> float:
        return max(lo, min(hi, v))

    for dp in sound_board.get("day_parts", []):
        dp["energy_target"]     = clamp(dp.get("energy_target", 0.5) + adj["energy"])
        dp["acousticness_target"] = clamp(dp.get("acousticness_target", 0.4) + adj["acousticness"])
        dp["tempo_target"]      = int(clamp(dp.get("tempo_target", 110) + adj["tempo"], 60, 200))
        # Adjust min/max for energy
        if "energy_min" in dp:
            dp["energy_min"] = clamp(dp["energy_min"] + adj["energy"])
        if "energy_max" in dp:
            dp["energy_max"] = clamp(dp["energy_max"] + adj["energy"])

    return sound_board


def _log_to_debug_file(content: str, tag: str) -> None:
    try:
        with open("azure_openai_debug.log", "a", encoding="utf-8") as f:
            f.write(f"\n=== {tag} ===\n{content}\n")
    except Exception:
        pass
