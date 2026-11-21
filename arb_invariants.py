"""Shared validation for the scanner's non-negotiable PM↔one-bookmaker rule."""
import math


_POLYMARKET_SOURCE = "polymarket"
_BOOKMAKER_SOURCES = frozenset({"1win", "betboom"})


def _source_id(value):
    return value.strip().casefold() if isinstance(value, str) else ""


def is_pm_bookmaker_pair(
    legs,
    *,
    source_key="source",
    source_kind_key=None,
    outcome_key="outcome",
    expected_bookmaker_source=None,
    allow_unregistered_bookmaker=False,
    require_pm_token=False,
    require_prices=False,
):
    """Return True only for one PM leg and one distinct bookmaker leg.

    When requested, scanner candidates also need a Polymarket token and finite
    decimal odds greater than 1. The helper intentionally accepts any
    non-empty non-Polymarket source so a future bookmaker can be added without
    permitting bookmaker-to-bookmaker comparisons.
    """
    if not isinstance(legs, (list, tuple)) or len(legs) != 2:
        return False
    if not all(isinstance(leg, dict) for leg in legs):
        return False

    sources = [_source_id(leg.get(source_key)) for leg in legs]
    if not all(sources) or sources.count(_POLYMARKET_SOURCE) != 1:
        return False

    pm_index = sources.index(_POLYMARKET_SOURCE)
    bookmaker_index = 1 - pm_index
    bookmaker_source = sources[bookmaker_index]
    if _POLYMARKET_SOURCE in bookmaker_source:
        return False
    if source_kind_key is not None:
        kinds = [
            _source_id(leg.get(source_kind_key)) for leg in legs
        ]
        if (kinds[pm_index] != "pm" or kinds[bookmaker_index] != "bc"):
            return False
    if (bookmaker_source not in _BOOKMAKER_SOURCES
            and not (allow_unregistered_bookmaker and source_kind_key is not None)):
        return False
    if (expected_bookmaker_source is not None
            and bookmaker_source != _source_id(expected_bookmaker_source)):
        return False

    raw_outcomes = [leg.get(outcome_key) for leg in legs]
    if not all(isinstance(outcome, str) for outcome in raw_outcomes):
        return False
    outcomes = [outcome.strip().casefold() for outcome in raw_outcomes]
    if not all(outcomes) or outcomes[0] == outcomes[1]:
        return False

    if require_pm_token:
        token = legs[pm_index].get("token")
        if not isinstance(token, str) or not token.strip():
            return False

    if require_prices:
        for leg in legs:
            try:
                odds = float(leg.get("o"))
            except (TypeError, ValueError):
                return False
            if not math.isfinite(odds) or odds <= 1.0:
                return False

    return True
