"""
web_scraper.py — Firecrawl-based brand website scraper.

Returns cleaned markdown text for use in brand analysis prompts.
Gracefully returns "" if the API key is missing or the scrape fails.
"""

import os
import logging

logger = logging.getLogger(__name__)


def scrape_brand_website(url: str, char_limit: int = 3000) -> str:
    """Scrape a brand website via Firecrawl and return truncated markdown."""
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
        return content[:char_limit].strip()
    except Exception as e:
        logger.warning("Firecrawl scrape failed for %s: %s", url, e)
        return ""
