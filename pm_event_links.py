"""Safe Polymarket event-page links derived from Gamma market metadata."""
from __future__ import annotations

import re


_POLYMARKET_EVENT_BASE = "https://polymarket.com/event/"
_SLUG_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,255}$")


def _valid_event_slug(value) -> str | None:
    slug = str(value or "").strip()
    return slug if _SLUG_RE.fullmatch(slug) else None


def polymarket_event_url_for_market(market, fallback_slug=None) -> str | None:
    """Return the parent event URL when Gamma includes it, else the market slug URL."""
    if not isinstance(market, dict):
        return None

    events = market.get("events")
    if isinstance(events, list):
        for event in events:
            if isinstance(event, dict):
                event_slug = _valid_event_slug(event.get("slug"))
                if event_slug:
                    return _POLYMARKET_EVENT_BASE + event_slug

    slug = _valid_event_slug(market.get("slug")) or _valid_event_slug(fallback_slug)
    return _POLYMARKET_EVENT_BASE + slug if slug else None
