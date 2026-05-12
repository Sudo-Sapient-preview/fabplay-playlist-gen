"""
brand_analysis.py — Azure OpenAI Call 1: brand inputs → Brand Profile JSON

Outputs a Brand Profile dict with Aaker personality scores, music baselines,
aesthetics, customer profile, and genre recommendations.
"""

import json
import logging
import os
import time

from openai import AzureOpenAI, RateLimitError, AuthenticationError, APIConnectionError

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are a brand strategist and music psychologist specializing in retail soundscaping.
Analyze the provided brand information and return ONLY a valid JSON object.
Your entire response must be valid JSON — you are using json_object response format.
Do not include any explanation, markdown, or code fences.

Source weighting rules:
- If BRAND GUIDELINES are provided, treat them as the primary source of truth — they override inferences from descriptions or website.
- If brand asset analysis (images, logos, mood boards) is provided, weight it heavily alongside the text description.
- Only fall back to inferring from business category / website when no assets are present.

Required output fields (all must be present):
{
  "brand_name": string,
  "brand_summary": string (2-3 sentence brand narrative),
  "sincerity": float 0.0-1.0,
  "excitement": float 0.0-1.0,
  "competence": float 0.0-1.0,
  "sophistication": float 0.0-1.0,
  "ruggedness": float 0.0-1.0,
  "price_positioning": string (value|mid_range|premium|luxury),
  "visual_aesthetic": string (brief descriptor e.g. rustic-warm, minimal-modern),
  "brand_adjectives": [string, string, string] (3 core adjectives),
  "customer_description": string,
  "customer_age_min": int,
  "customer_age_max": int,
  "customer_lifestyle_tags": [string],
  "music_energy_baseline": float 0.0-1.0,
  "music_valence_baseline": float 0.0-1.0,
  "music_tempo_baseline": int (BPM),
  "recommended_genres": [string],
  "avoid_genres": [string],
  "confidence_score": float 0.0-1.0
}

Aaker Brand Personality Framework mapping guide:
- Sincerity (warm, honest, genuine): high → acoustic, folk, major key, lower energy
- Excitement (bold, spirited, imaginative): high → high energy, high tempo, pop/electronic
- Competence (reliable, intelligent, successful): high → structured jazz/orchestral, consistent tempo
- Sophistication (elegant, refined, prestigious): high → high instrumentalness, classical/jazz, low loudness
- Ruggedness (tough, outdoorsy, rugged): high → rock/country, raw sound, higher loudness
"""


def _build_user_prompt(inputs: dict) -> str:
    asset_analysis = inputs.get("asset_analysis", "")
    has_brand_guidelines = inputs.get("has_brand_guidelines", False)

    lines = []

    # Asset analysis goes first so it anchors the model's interpretation
    if asset_analysis:
        if has_brand_guidelines:
            lines.append(
                "=== BRAND GUIDELINES PROVIDED (PRIMARY SOURCE — override inferences below) ===\n"
                + asset_analysis
                + "\n=== END BRAND GUIDELINES ==="
            )
        else:
            lines.append(
                "=== BRAND ASSET ANALYSIS (weight heavily alongside description below) ===\n"
                + asset_analysis
                + "\n=== END ASSET ANALYSIS ==="
            )
        lines.append("")

    scraped = inputs.get("scraped_content", "").strip()
    if scraped:
        lines.append(
            "=== WEBSITE CONTENT (scraped — use to inform all brand inferences) ===\n"
            + scraped
            + "\n=== END WEBSITE CONTENT ==="
        )
        lines.append("")

    lines += [
        f"Brand Name: {inputs.get('brand_name', '')}",
        f"Business Category: {inputs.get('business_category', '')}",
        f"Website URL: {inputs.get('website_url', 'Not provided')}",
        f"Brand Description: {inputs.get('brand_description', 'Not provided')}",
        f"Customer Description: {inputs.get('customer_description', 'Not provided')}",
        f"Customer Segment: {inputs.get('customer_segment', 'Not provided')}",
        f"Customer Age Range: {inputs.get('age_min', '')} – {inputs.get('age_max', '')}",
        f"Lifestyle Tags: {', '.join(inputs.get('lifestyle_tags', []))}",
        f"Must-Include Genres: {inputs.get('include_genres', 'None')}",
        f"Must-Exclude Genres: {inputs.get('exclude_genres', 'None')}",
        f"Must-Include Artists: {inputs.get('include_artists', 'None')}",
        f"Must-Exclude Artists: {inputs.get('exclude_artists', 'None')}",
        f"Filter Explicit Content: {inputs.get('filter_explicit', True)}",
        f"Additional Music Notes: {inputs.get('music_notes', 'None')}",
    ]
    return "\n".join(lines)


def get_brand_profile(
    inputs: dict,
    chat_client: AzureOpenAI,
    deployment: str,
    max_retries: int = 3,
) -> dict:
    """
    Call Azure OpenAI to produce a Brand Profile JSON from collected user inputs.

    Args:
        inputs:       Dict of all collected terminal prompt values.
        chat_client:  Configured AzureOpenAI client.
        deployment:   Chat model deployment name (e.g. "gpt-4o").
        max_retries:  Max retry attempts on rate-limit errors.

    Returns:
        Parsed Brand Profile dict.
    """
    user_prompt = _build_user_prompt(inputs)

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
                profile = json.loads(raw)
                # Inject brand_name if the model omitted it
                if "brand_name" not in profile:
                    profile["brand_name"] = inputs.get("brand_name", "")
                return profile

            except json.JSONDecodeError as e:
                logger.error("Brand Profile JSON parse error (attempt %d): %s", attempt, e)
                logger.debug("Raw response: %s", raw)
                _log_to_debug_file(raw, "brand_profile_parse_error")
                if attempt == max_retries:
                    raise RuntimeError(
                        "Failed to parse Brand Profile JSON after "
                        f"{max_retries} attempts. See azure_openai_debug.log."
                    ) from e
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

    raise RuntimeError("Brand Profile generation failed after all retries.")


def _log_to_debug_file(content: str, tag: str) -> None:
    try:
        with open("azure_openai_debug.log", "a", encoding="utf-8") as f:
            f.write(f"\n=== {tag} ===\n{content}\n")
    except Exception:
        pass
