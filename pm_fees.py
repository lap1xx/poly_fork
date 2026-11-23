"""Read-only Polymarket per-market taker fee metadata and fee curve helpers."""
from __future__ import annotations

import math
import threading
import time
from urllib.parse import quote

import requests

CLOB_API = "https://clob.polymarket.com"
PM_FEE_CACHE_TTL_SEC = 300.0
PM_FEE_API_TIMEOUT = (2.0, 4.0)
_CACHE_LOCK = threading.RLock()
_CACHE: dict[str, dict] = {}


def normalize_fee_schedule(payload):
    """Validate public CLOB market-info fields; return None if incomplete."""
    if not isinstance(payload, dict):
        return None
    schedule = payload.get("feeSchedule")
    enabled = payload.get("feesEnabled")
    if enabled is not None and not isinstance(enabled, bool):
        return None
    if not isinstance(schedule, dict):
        if enabled is False:
            return {
                "fees_enabled": False, "rate": 0.0, "exponent": 1.0,
                "source": "clob_market_info",
            }
        return None
    try:
        rate = float(schedule.get("rate"))
        exponent = float(schedule.get("exponent"))
    except (TypeError, ValueError, OverflowError):
        return None
    if (not math.isfinite(rate) or not math.isfinite(exponent)
            or rate < 0.0 or rate > 1.0 or exponent <= 0.0 or exponent > 10.0):
        return None
    fees_enabled = enabled if enabled is not None else rate > 0.0
    if not fees_enabled and rate != 0.0:
        return None
    return {
        "fees_enabled": fees_enabled,
        "rate": rate if fees_enabled else 0.0,
        "exponent": exponent,
        "source": "clob_market_info",
    }


def fetch_fee_schedule(condition_id, *, now=None, force=False):
    """Fetch and briefly cache per-market CLOB fees; no fixed-fee fallback."""
    condition_id = str(condition_id or "").strip()
    if not condition_id or len(condition_id) > 256:
        return None
    cache_now = time.monotonic() if now is None else float(now)
    with _CACHE_LOCK:
        cached = _CACHE.get(condition_id)
        if (not force and cached
                and cache_now - cached["cached_at"] <= PM_FEE_CACHE_TTL_SEC):
            return dict(cached["schedule"])
    try:
        response = requests.get(
            CLOB_API + "/markets/" + quote(condition_id, safe=""),
            timeout=PM_FEE_API_TIMEOUT,
        )
        response.raise_for_status()
        schedule = normalize_fee_schedule(response.json())
    except Exception:
        return None
    if schedule is None:
        return None
    with _CACHE_LOCK:
        _CACHE[condition_id] = {"cached_at": cache_now, "schedule": dict(schedule)}
    return schedule


def fee_per_share(price, rate, exponent):
    """Continuous fee estimate per share: rate * (p * (1-p)) ** exponent."""
    try:
        price, rate, exponent = float(price), float(rate), float(exponent)
    except (TypeError, ValueError, OverflowError):
        return None
    if (not all(math.isfinite(x) for x in (price, rate, exponent))
            or not 0.0 < price < 1.0 or rate < 0.0 or exponent <= 0.0):
        return None
    return rate * (price * (1.0 - price)) ** exponent


def clear_fee_cache():
    """Clear cached metadata; intended for deterministic tests and reloads."""
    with _CACHE_LOCK:
        _CACHE.clear()
