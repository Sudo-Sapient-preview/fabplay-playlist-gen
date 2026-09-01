import base64
import json
import logging
import os
import time
from typing import Iterable

import fitz
import requests
from openai import OpenAI, RateLimitError

from api.db import fetch_all_songs

logger = logging.getLogger(__name__)


def _chat_with_retry(client, **kwargs):
    for attempt in range(4):
        try:
            return client.chat.completions.create(**kwargs)
        except RateLimitError:
            if attempt == 3:
                raise
            wait = 2 ** attempt * 3
            logger.warning("Rate limited, retrying in %ss (attempt %d/4)", wait, attempt + 1)
            time.sleep(wait)
    raise RuntimeError("Unreachable")


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
IMPORTANT: suggested_activities, suggested_customer_types, and suggested_lifestyle must be based on the BUSINESS CATEGORY only (e.g. hotel, cafe, retail) - not the brand name.
The brand name is only used to infer customer_segment and brand_description.
suggested_activities: 4-6 things customers typically do in this type of space (e.g. for hotel: "Check in and relax", "Dine at the restaurant", "Use pool or spa", "Attend meetings")
suggested_customer_types: 4-6 types of people who visit this type of place (e.g. for hotel: "Business travelers", "Couples on holiday", "Families", "Solo travelers")
suggested_lifestyle: 4-6 lifestyle descriptors typical for this type of venue (e.g. for hotel: "Urban professional", "Leisure traveler", "Health-conscious")"""

GUIDELINE_KEYWORDS = {
    "guideline",
    "guide",
    "brand",
    "style",
    "identity",
    "manual",
    "standard",
}

_ASSET_TEXT_PROMPT = (
    "You are a brand strategist. Extract all brand identity signals from this document: "
    "visual style, tone of voice, personality, target customer, aesthetic values, "
    "atmosphere/music cues, colour palette, and any explicit brand rules. Be specific."
)

_ASSET_IMAGE_PROMPT = (
    "You are a brand strategist. Analyse this brand asset image and extract brand identity signals: "
    "visual style, colour palette, mood, personality, aesthetic, and music/atmosphere cues it suggests."
)


OPENROUTER_BASE_URL = os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
DEFAULT_MODEL = "openai/gpt-4o-mini"


def chat_client() -> tuple[OpenAI, str]:
    """OpenRouter chat client (OpenAI-compatible)."""
    api_key = os.getenv("OPENROUTER_API_KEY", "").strip()
    model = os.getenv("OPENROUTER_MODEL", "").strip() or DEFAULT_MODEL
    if not api_key:
        raise RuntimeError("OPENROUTER_API_KEY not set")
    return OpenAI(api_key=api_key, base_url=OPENROUTER_BASE_URL), model


def scrape_website(url: str, max_chars: int = 8000) -> str:
    api_key = os.getenv("FIRECRAWL_API_KEY", "")
    if not api_key or not url:
        return ""
    try:
        resp = requests.post(
            "https://api.firecrawl.dev/v1/scrape",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={"url": url, "formats": ["markdown"]},
            timeout=20,
        )
        resp.raise_for_status()
        data = resp.json()
        content = (data.get("data") or {}).get("markdown") or ""
        return content[:max_chars].strip()
    except Exception as exc:
        logger.warning("Firecrawl scrape failed for %s: %s", url, exc)
        return ""


def quick_analyze(brand_name: str, category: str, website_url: str = "", website_content: str = "", brand_description: str = "") -> dict:
    client, deployment = chat_client()
    user_parts = [
        f"Brand: {brand_name}",
        f"Category: {category or 'not provided'}",
        f"Website: {website_url or 'not provided'}",
    ]
    if brand_description:
        user_parts.append(f"Brand Description: {brand_description}")
    if website_content:
        user_parts.append(f"\nWebsite content (scraped):\n{website_content}")
    response = _chat_with_retry(client, 
        model=deployment,
        max_tokens=600,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": QUICK_ANALYZE_PROMPT},
            {"role": "user", "content": "\n".join(user_parts)},
        ],
    )
    return json.loads(response.choices[0].message.content)


def music_recs_from_content(content: str) -> dict:
    """Run music genre recommendations against arbitrary text (e.g. scraped website)."""
    if not content:
        return {"recommended_genres": [], "avoid_genres": [], "music_notes": ""}
    client, deployment = chat_client()
    try:
        response = _chat_with_retry(client, 
            model=deployment,
            max_tokens=300,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": _music_recommendations_prompt()},
                {"role": "user", "content": content},
            ],
        )
        return json.loads(response.choices[0].message.content)
    except Exception as exc:
        logger.warning("music_recs_from_content failed: %s", exc)
        return {"recommended_genres": [], "avoid_genres": [], "music_notes": ""}


def _music_recommendations_prompt() -> str:
    db_genres = sorted({song.get("genre") for song in fetch_all_songs() if song.get("genre")})
    genre_list = ", ".join(
        {
            "hip_hop": "Hip Hop",
            "soul_funk": "Soul/Funk",
            "indi_pop": "Indi Pop",
            "traditional_folk": "Traditional Folk",
            "edm": "EDM",
        }.get(genre, genre.replace("_", " ").title())
        for genre in db_genres
    )
    return f"""\
You are a brand strategist. Based on this brand asset analysis, return ONLY a valid JSON object:
{{
  "recommended_genres": ["genre1", "genre2", "genre3"],
  "avoid_genres": ["genre1", "genre2"],
  "music_notes": "Brief note about music direction (1-2 sentences)"
}}
Only include genres from this list: {genre_list}"""


def _analyze_pdf(client: OpenAI, deployment: str, filename: str, content: bytes) -> str:
    doc = fitz.open(stream=content, filetype="pdf")
    try:
        text = "\n".join(page.get_text() for page in doc)
    finally:
        doc.close()
    response = _chat_with_retry(client, 
        model=deployment,
        max_tokens=600,
        messages=[
            {"role": "system", "content": _ASSET_TEXT_PROMPT},
            {"role": "user", "content": f"File: {filename}\n\n{text[:30000]}"},
        ],
    )
    return response.choices[0].message.content


def _analyze_image(client: OpenAI, deployment: str, filename: str, content: bytes) -> str:
    lower = filename.lower()
    mime = (
        "image/jpeg"
        if lower.endswith((".jpg", ".jpeg"))
        else "image/png"
        if lower.endswith(".png")
        else "image/webp"
        if lower.endswith(".webp")
        else "image/gif"
    )
    b64 = base64.b64encode(content).decode()
    response = _chat_with_retry(client, 
        model=deployment,
        max_tokens=400,
        messages=[
            {"role": "system", "content": _ASSET_IMAGE_PROMPT},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": f"Brand asset: {filename}"},
                    {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
                ],
            },
        ],
    )
    return response.choices[0].message.content


def analyze_files(files: Iterable[tuple[str, bytes]]) -> dict:
    client, deployment = chat_client()
    analyses: list[str] = []
    filenames: list[str] = []

    for filename, content in files:
        filenames.append(filename)
        lower = (filename or "").lower()
        try:
            if lower.endswith(".pdf"):
                analyses.append(f"[{filename}]:\n{_analyze_pdf(client, deployment, filename, content)}")
            elif any(lower.endswith(ext) for ext in (".jpg", ".jpeg", ".png", ".webp", ".gif")):
                analyses.append(f"[{filename}]:\n{_analyze_image(client, deployment, filename, content)}")
        except Exception as exc:
            analyses.append(f"[{filename}]: (analysis failed - {exc})")

    asset_analysis = "\n\n".join(analyses)
    music_recs: dict = {
        "recommended_genres": [],
        "avoid_genres": [],
        "music_notes": "",
    }

    if asset_analysis:
        try:
            response = _chat_with_retry(client, 
                model=deployment,
                max_tokens=300,
                response_format={"type": "json_object"},
                messages=[
                    {"role": "system", "content": _music_recommendations_prompt()},
                    {"role": "user", "content": asset_analysis},
                ],
            )
            music_recs = json.loads(response.choices[0].message.content)
        except Exception:
            pass

    has_guidelines = any(
        keyword in filename.lower()
        for filename in filenames
        for keyword in GUIDELINE_KEYWORDS
    )

    return {
        "asset_analysis": asset_analysis,
        "recommended_genres": music_recs.get("recommended_genres", []),
        "avoid_genres": music_recs.get("avoid_genres", []),
        "music_notes": music_recs.get("music_notes", ""),
        "has_brand_guidelines": has_guidelines,
    }

