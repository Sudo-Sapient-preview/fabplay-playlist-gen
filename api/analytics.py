"""Thin PostHog wrapper used by API views.

Safe to call even when PostHog is disabled or misconfigured — capture
failures are logged and never raised to request handlers.
"""

from __future__ import annotations

import logging
from typing import Any

from django.conf import settings

logger = logging.getLogger(__name__)

_client = None
_client_failed = False


def _get_client():
    global _client, _client_failed
    if _client is not None or _client_failed:
        return _client
    if not getattr(settings, "POSTHOG_ENABLED", False):
        return None
    api_key = (getattr(settings, "POSTHOG_API_KEY", "") or "").strip()
    if not api_key:
        return None
    try:
        from posthog import Posthog

        _client = Posthog(
            project_api_key=api_key,
            host=getattr(settings, "POSTHOG_HOST", "https://us.i.posthog.com"),
            sync_mode=False,
        )
    except Exception:
        logger.exception("Failed to initialize PostHog client")
        _client_failed = True
        _client = None
    return _client


def identify(user_id: str, properties: dict[str, Any] | None = None) -> None:
    client = _get_client()
    if not client or not user_id:
        return
    try:
        client.identify(distinct_id=str(user_id), properties=properties or {})
    except Exception:
        logger.exception("PostHog identify failed")


def capture(
    distinct_id: str | None,
    event: str,
    properties: dict[str, Any] | None = None,
) -> None:
    client = _get_client()
    if not client or not event:
        return
    props = dict(properties or {})
    props.setdefault("source", "backend")
    try:
        client.capture(
            distinct_id=str(distinct_id or "anonymous"),
            event=event,
            properties=props,
        )
    except Exception:
        logger.exception("PostHog capture failed for event=%s", event)


def shutdown() -> None:
    client = _get_client()
    if not client:
        return
    try:
        client.shutdown()
    except Exception:
        logger.exception("PostHog shutdown failed")
