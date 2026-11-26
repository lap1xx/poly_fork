"""Evidence-based event phase classification for Polymarket markets.

An event is LIVE only when the source provides an explicit live signal. A future
start time may identify PREMATCH; a past/missing start without an explicit state
is UNKNOWN rather than an assumption that the event is in progress.
"""
from __future__ import annotations

import math
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


# Keep just the provider evidence necessary for a later, dynamic re-evaluation.
# A normalized UNKNOWN alone cannot distinguish an ordinary past start from a
# terminal event or contradictory source flags.
_PM_EVIDENCE_KEYS = frozenset(_FLAG_KEYS + _STATUS_KEYS + _START_KEYS + (
    "closed", "resolved", "cancelled", "canceled", "active", "ended", "finished",
))
ONEWIN_PHASE_MAX_AGE_SECONDS = 210.0  # 180s catalog interval plus 30s grace
PM_PHASE_MAX_AGE_SECONDS = 120.0


def capture_pm_phase_evidence(market: Any, event: Any = None, *, received_at=None) -> dict:
    def selected(source):
        return {key: source[key] for key in _PM_EVIDENCE_KEYS if key in source} \
            if isinstance(source, dict) else {}
    return {"market": selected(market), "event": selected(event), "received_at": received_at}


def _safe_pm_evidence(market: dict, event: dict) -> bool:
    sources = (market, event)
    bool_keys = _FLAG_KEYS + ("closed", "resolved", "cancelled", "canceled", "active", "ended", "finished")
    for source in sources:
        for key in bool_keys:
            if key in source and source[key] is not None and parse_bool_flag(source[key]) is None:
                return False
        for key in ("closed", "resolved", "cancelled", "canceled", "ended", "finished"):
            if parse_bool_flag(source.get(key)) is True:
                return False
        if "active" in source and parse_bool_flag(source["active"]) is False:
            return False
        for key in _STATUS_KEYS:
            status = _status(source.get(key))
            if status and status not in (_LIVE_STATUSES | _PREMATCH_STATUSES):
                return False
        for key in _START_KEYS:
            if source.get(key) not in (None, "") and _parse_start(source[key]) is None:
                return False
    flags = [parse_bool_flag(source[key]) for source in sources for key in _FLAG_KEYS
             if key in source and parse_bool_flag(source[key]) is not None]
    statuses = [_status(source.get(key)) for source in sources for key in _STATUS_KEYS]
    live = any(status in _LIVE_STATUSES for status in statuses)
    prematch = any(status in _PREMATCH_STATUSES for status in statuses)
    return not ((True in flags and False in flags) or (live and prematch)
                or (live and False in flags) or (prematch and True in flags))


def _onewin_start(value: Any) -> Optional[datetime]:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        try:
            number = float(value)
            if not math.isfinite(number) or number <= 0:
                return None
            if number > 10_000_000_000:
                number /= 1000.0
            return datetime.fromtimestamp(number, timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    return _parse_start(value)


def _fresh_receipt(value: Any, now: datetime, max_age=ONEWIN_PHASE_MAX_AGE_SECONDS) -> bool:
    if isinstance(value, bool):
        return False
    try:
        timestamp = float(value)
        age = now.timestamp() - timestamp
        return math.isfinite(timestamp) and timestamp > 0 and 0 <= age <= max_age
    except (TypeError, ValueError, OverflowError, OSError):
        return False


def resolve_event_phase(market: Any, onewin: Any = None, *, now: Optional[datetime] = None) -> dict:
    """Resolve phase conservatively; the 1win input must be a verified binding.

    Legacy normalized markets have no original evidence, so cannot use the
    fallback. Receipt times are local observations, not provider latency.
    """
    market = market if isinstance(market, dict) else {}
    reference = now or datetime.now(timezone.utc)
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=timezone.utc)
    reference = reference.astimezone(timezone.utc)
    evidence = market.get("_pm_phase_evidence")
    if not (isinstance(evidence, dict) and isinstance(evidence.get("market"), dict)
            and isinstance(evidence.get("event"), dict)):
        phase = classify_event_phase(market, now=reference)
        return {"phase": phase, "source": "pm" if phase != "unknown" else "unknown", "reason": "legacy_pm"}
    if not _fresh_receipt(evidence.get("received_at"), reference, PM_PHASE_MAX_AGE_SECONDS):
        return {"phase": "unknown", "source": "unknown", "reason": "pm_evidence_stale"}
    raw_market, raw_event = evidence["market"], evidence["event"]
    pm_phase = classify_event_phase(raw_market, raw_event, now=reference)
    if not _safe_pm_evidence(raw_market, raw_event):
        return {"phase": "unknown", "source": "unknown", "reason": "pm_conflict_or_invalid"}
    result = {"phase": pm_phase, "source": "pm" if pm_phase != "unknown" else "unknown", "reason": "pm"}
    if not isinstance(onewin, dict):
        return result
    catalog_at = onewin.get("catalog_at")
    catalog_fresh = _fresh_receipt(catalog_at, reference)
    ws_at = onewin.get("ws_service_at")
    if ws_at is not None:
        # A malformed/future timestamp must never resurrect an older catalog state.
        try:
            ws_time = float(ws_at) if not isinstance(ws_at, bool) else float("nan")
        except (ValueError, TypeError, OverflowError):
            return result
        if not math.isfinite(ws_time) or ws_time <= 0 or ws_time > reference.timestamp():
            return result
        try:
            catalog_time = float(catalog_at) if not isinstance(catalog_at, bool) else float("nan")
        except (ValueError, TypeError, OverflowError):
            catalog_time = float("nan")
        if not math.isfinite(catalog_time) or ws_time >= catalog_time:
            if not _fresh_receipt(ws_time, reference):
                return result
            services = [onewin.get("ws_service")]
        else:
            if not catalog_fresh:
                return result
            services = [onewin.get("service"), onewin.get("serviceV2")]
    else:
        if not catalog_fresh:
            return result
        services = [onewin.get("service"), onewin.get("serviceV2")]
    services = [s for s in services if s is not None and s != ""]
    if not services or any(not isinstance(s, str) for s in services):
        return result
    statuses = [_status(s) for s in services]
    if any(s not in {"prematch", "live"} for s in statuses):
        return result
    if len(set(statuses)) != 1:
        return {"phase": "unknown", "source": "conflict", "reason": "onewin_service_conflict"}
    ow_phase = statuses[0]
    if (pm_phase == "live" and ow_phase == "prematch") or (pm_phase == "prematch" and ow_phase == "live"):
        return {"phase": "unknown", "source": "conflict", "reason": "source_phase_conflict"}
    # Start time is independently timestamped by the catalog: a fresh WS status
    # can veto a contradiction, but cannot make an old/missing start time fresh.
    if not catalog_fresh:
        return result
    start = _onewin_start(onewin.get("startAt"))
    if start is None:
        return result
    if ow_phase == "prematch" and start <= reference:
        if pm_phase == "prematch":
            return {"phase": "unknown", "source": "conflict", "reason": "onewin_start_passed"}
        return result
    if pm_phase == "unknown" and ow_phase == "prematch":
        result = {"phase": "prematch", "source": "1win", "reason": "onewin_prematch"}
        pm_start = next((_parse_start(source.get(key)) for source in (raw_market, raw_event)
                         for key in _START_KEYS if _parse_start(source.get(key)) is not None), None)
        if pm_start is not None:
            result["start_difference_sec"] = (start - pm_start).total_seconds()
    return result
