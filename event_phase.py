"""Evidence-based event phase classification for Polymarket markets.

An event is LIVE only when the source provides an explicit live signal. A future
start time may identify PREMATCH; a past/missing start without an explicit state
is UNKNOWN rather than an assumption that the event is in progress.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

VALID_PHASES = frozenset({"live", "prematch", "unknown"})

_LIVE_STATUSES = {
    "live", "inplay", "in_play", "in-play", "in play", "in progress", "in_progress",
    "ongoing", "playing", "started", "матч идет", "матч идёт", "в игре",
}
_PREMATCH_STATUSES = {
    "prematch", "pre-match", "pre_match", "pre match", "scheduled", "upcoming",
    "not started", "not_started", "before", "ожидание", "запланирован",
}
_TERMINAL_STATUSES = {
    "finished", "ended", "final", "completed", "closed", "resolved",
    "cancelled", "canceled", "postponed", "abandoned", "отменен", "отменён",
    "завершен", "завершён",
}
_FLAG_KEYS = ("live", "isLive", "is_live", "inplay", "inPlay", "isInPlay")
_STATUS_KEYS = ("status", "eventStatus", "event_status", "gameStatus", "state")
_START_KEYS = ("start", "eventStartTime", "gameStartTime", "startTime", "start_time")


def parse_bool_flag(value: Any) -> Optional[bool]:
    """Parse a provider boolean without treating arbitrary non-empty strings as true."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "1", "yes", "y", "да"}:
            return True
        if normalized in {"false", "0", "no", "n", "нет"}:
            return False
    return None


def _status(value: Any) -> str:
    return " ".join(str(value or "").strip().lower().replace("-", " ").split())


def _parse_start(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip().replace("Z", "+00:00").replace(" ", "T", 1)
    try:
        parsed = datetime.fromisoformat(raw)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def classify_event_phase(
    market: Any,
    event: Any = None,
    *,
    now: Optional[datetime] = None,
) -> str:
    """Return ``live``, ``prematch`` or ``unknown`` using explicit source evidence.

    ``market`` and ``event`` are provider objects. Explicit LIVE flags/statuses
    take precedence. PREMATCH is returned for a clear pre-start status or a
    future start time, unless the market/event is terminal/inactive. A past start
    time, map/set/game slug fragment, or missing state never implies LIVE.
    """
    if not isinstance(market, dict):
        market = {}
    if not isinstance(event, dict):
        event = {}

    existing = str(market.get("phase") or "").strip().lower()
    if market.get("_phase_classified") is True and existing in VALID_PHASES:
        return existing

    sources = (market, event)
    statuses = [_status(source.get(key)) for source in sources for key in _STATUS_KEYS]
    statuses = [status for status in statuses if status]

    terminal = any(
        parse_bool_flag(source.get(key)) is True
        for source in sources for key in ("closed", "resolved", "cancelled", "canceled")
        if key in source
    ) or any(status in _TERMINAL_STATUSES for status in statuses)
    inactive = any(
        parse_bool_flag(source.get("active")) is False
        for source in sources if "active" in source
    )
    if terminal or inactive:
        return "unknown"

    explicit_flags = [
        parse_bool_flag(source.get(key))
        for source in sources for key in _FLAG_KEYS if key in source
    ]
    explicit_flags = [flag for flag in explicit_flags if flag is not None]
    if True in explicit_flags and False in explicit_flags:
        return "unknown"

    live_status = any(status in _LIVE_STATUSES for status in statuses)
    prematch_status = any(status in _PREMATCH_STATUSES for status in statuses)
    if live_status and prematch_status:
        return "unknown"
    if (live_status and False in explicit_flags) or (prematch_status and True in explicit_flags):
        return "unknown"
    if live_status:
        return "live"
    if True in explicit_flags:
        return "live"

    if prematch_status:
        return "prematch"

    # A future scheduled start is useful PREMATCH evidence; a past start is not
    # evidence of LIVE because an event could have ended, moved or been delayed.
    start = None
    for source in sources:
        for key in _START_KEYS:
            start = _parse_start(source.get(key))
            if start is not None:
                break
        if start is not None:
            break
    if start is not None:
        reference = now or datetime.now(timezone.utc)
        if reference.tzinfo is None:
            reference = reference.replace(tzinfo=timezone.utc)
        if start > reference.astimezone(timezone.utc):
            return "prematch"

    return "unknown"
