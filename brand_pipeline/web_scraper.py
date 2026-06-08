"""
web_scraper.py — Firecrawl-based brand website scraper.

Returns cleaned markdown text for use in brand analysis prompts.
Gracefully returns "" if the API key is missing or the scrape fails.
"""

import os
import html
import logging

logger = logging.getLogger(__name__)


def scrape_brand_website(url: str, char_limit: int | None = None) -> str:
    """Scrape a brand website via Firecrawl and return markdown. Pass char_limit to truncate."""
    if not url or not url.startswith(("http://", "https://")):
        return ""
    api_key = os.getenv("FIRECRAWL_API_KEY", "")
    if not api_key:
        logger.warning("FIRECRAWL_API_KEY not set — skipping web scrape for %s", url)
        return ""
    try:
        from firecrawl import FirecrawlApp
        app = FirecrawlApp(api_key=api_key)
        result = app.scrape_url(url, params={"formats": ["markdown"], "timeout": 15000})
        content = (result or {}).get("markdown", "") or ""
        # Firecrawl markdown can carry HTML entities (&amp;, &#39;, &quot;) straight
        # from the page source; unescape them so the LLM sees "R&B", not "R&amp;B",
        # and never echoes the entity into brand fields like recommended_genres.
        content = html.unescape(content)
        return (content[:char_limit] if char_limit else content).strip()
    except Exception as e:
        logger.warning("Firecrawl scrape failed for %s: %s", url, e)
        return ""
