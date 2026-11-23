"""Neutral read-only contract for bookmaker quote adapters.

This module deliberately does not adapt or replace the existing 1win and
BetBoom facades. It defines the boundary they can implement incrementally.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import math
from typing import Callable, Optional, Protocol, Tuple, runtime_checkable


UpdateCallback = Callable[[str, str, float], None]


class QuoteState(str, Enum):
    OPEN = "open"
    CLOSED = "closed"
    SUSPENDED = "suspended"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class MarketOutcome:
    """One Polymarket outcome and its stable token identifier."""

    outcome_id: str
    label: str
    token_id: str


@dataclass(frozen=True)
class BinaryMarket:
    """Normalized binary PM market; period is explicit and never a wildcard."""

    market_key: str
    market_kind: str
    period: str
    outcomes: Tuple[MarketOutcome, MarketOutcome]
    event_key: Optional[str] = None
    start_at: Optional[float] = None
    event_url: Optional[str] = None

    def __post_init__(self):
        if not self.market_key.strip():
            raise ValueError("market_key must not be empty")
        if not self.market_kind.strip():
            raise ValueError("market_kind must not be empty")
        if not self.period.strip():
            raise ValueError("period must be explicit; it is not a wildcard")
        if len(self.outcomes) != 2:
            raise ValueError("a binary market must have exactly two outcomes")
        if any(not item.outcome_id.strip() or not item.token_id.strip()
               for item in self.outcomes):
            raise ValueError("each outcome requires an outcome_id and token_id")
        if len({item.outcome_id for item in self.outcomes}) != 2:
            raise ValueError("binary outcome IDs must be distinct")
        if len({item.token_id for item in self.outcomes}) != 2:
            raise ValueError("binary PM token IDs must be distinct")


@dataclass(frozen=True)
class Binding:
    """Strict binding from one PM market to one market of one adapter."""

    adapter_id: str
    provider_event_id: str
    provider_market_id: str
    market_kind: str
    period: str
    # Pairs of (PM outcome_id, provider outcome_id); never inferred at quote time.
    outcome_map: Tuple[Tuple[str, str], Tuple[str, str]]
    match_status: str = "matched"
    catalog_revision: Optional[str] = None
    bound_at_epoch: Optional[float] = None
    event_url: Optional[str] = None


@dataclass(frozen=True)
class QuoteSnapshot:
    """A single provider quote with local receipt clocks kept separately."""

    adapter_id: str
    provider_event_id: str
    provider_market_id: str
    outcome_id: str
    price: float
    state: QuoteState
    received_at_monotonic: float
    received_at_epoch: float
    provider_timestamp: Optional[float] = None
    raw_market_name: Optional[str] = None
    raw_outcome_name: Optional[str] = None


@dataclass(frozen=True)
class QuotePair:
    """An atomic pair of opposite outcomes from exactly one provider market."""

    adapter_id: str
    provider_event_id: str
    provider_market_id: str
    quotes: Tuple[QuoteSnapshot, QuoteSnapshot]


@dataclass(frozen=True)
class AdapterHealth:
    """Non-sensitive adapter diagnostics; never contains account/auth payloads."""

    adapter_id: str
    connected: bool
    ready: bool
    catalog_complete: bool
    discovered_events: int = 0
    bound_events: int = 0
    fresh_pairs: int = 0
    stale_pairs: int = 0
    last_message_monotonic: Optional[float] = None
    last_valid_quote_monotonic: Optional[float] = None
    ttl_seconds: Optional[float] = None
    error_code: Optional[str] = None


@runtime_checkable
class BookmakerAdapter(Protocol):
    """Read-only lifecycle and quote interface; intentionally no bet methods."""

    @property
    def adapter_id(self) -> str:
        """Stable source label used to isolate bindings and quote pairs."""
        ...

    def start(self) -> None:
        """Start only the read-only catalog/quote transport."""
        ...

    def stop(self) -> None:
        """Stop owned read-only transport resources."""
        ...

    def wait_ready(self, timeout: float) -> bool:
        """Wait at most ``timeout`` seconds for usable read-only data."""
        ...

    def bind(self, market: BinaryMarket) -> Optional[Binding]:
        """Strictly bind one PM binary market or return ``None``."""
        ...

    def get_quote_pair(self, binding: Binding) -> Optional[QuotePair]:
        """Return both sides of this binding atomically, or ``None``."""
        ...

    def set_update_callback(self, callback: Optional[UpdateCallback]) -> None:
        """Register a lightweight (adapter, provider event, local receipt) callback."""
        ...

    def health(self) -> AdapterHealth:
        """Return transport/catalog/quote health without sensitive account data."""
        ...


def _is_nonempty_string(value) -> bool:
    return isinstance(value, str) and bool(value.strip())


def validate_binding(market: BinaryMarket, binding: Optional[Binding]) -> bool:
    """Fail closed unless the binding covers exactly this market and two sides."""
    if not isinstance(market, BinaryMarket) or not isinstance(binding, Binding):
        return False
    if binding.match_status != "matched":
        return False
    if not all(_is_nonempty_string(value) for value in (
        binding.adapter_id, binding.provider_event_id,
        binding.provider_market_id, binding.market_kind, binding.period,
    )):
        return False
    if binding.market_kind != market.market_kind or binding.period != market.period:
        return False
    if not isinstance(binding.outcome_map, tuple) or len(binding.outcome_map) != 2:
        return False
    if any(not isinstance(pair, tuple) or len(pair) != 2
           for pair in binding.outcome_map):
        return False
    pm_ids = [pair[0] for pair in binding.outcome_map]
    provider_ids = [pair[1] for pair in binding.outcome_map]
    expected_pm_ids = {outcome.outcome_id for outcome in market.outcomes}
    return (
        len(pm_ids) == 2 and len(provider_ids) == 2
        and set(pm_ids) == expected_pm_ids
        and len(set(pm_ids)) == 2 and len(set(provider_ids)) == 2
        and all(isinstance(value, str) and value.strip()
                for value in pm_ids + provider_ids)
    )


def validate_quote_pair(
    market: BinaryMarket,
    binding: Binding,
    pair: Optional[QuotePair],
    *,
    now_monotonic: float,
    max_age_seconds: float,
) -> bool:
    """Require two open, finite, fresh quotes from the exact selected adapter.

    Freshness is based only on local monotonic receipt time; provider timestamps
    are preserved as diagnostics and cannot rejuvenate a quote.
    """
    if not validate_binding(market, binding) or pair is None:
        return False
    try:
        now_monotonic = float(now_monotonic)
        max_age_seconds = float(max_age_seconds)
    except (TypeError, ValueError, OverflowError):
        return False
    if not math.isfinite(now_monotonic) or not math.isfinite(max_age_seconds):
        return False
    if max_age_seconds <= 0:
        return False
    if (not isinstance(pair, QuotePair)
            or not isinstance(pair.quotes, tuple)
            or pair.adapter_id != binding.adapter_id
            or pair.provider_event_id != binding.provider_event_id
            or pair.provider_market_id != binding.provider_market_id
            or len(pair.quotes) != 2):
        return False

    expected_provider_ids = {provider_id for _, provider_id in binding.outcome_map}
    observed_outcome_ids = set()
    for quote in pair.quotes:
        if not isinstance(quote, QuoteSnapshot):
            return False
        if not all(_is_nonempty_string(value) for value in (
            quote.adapter_id, quote.provider_event_id,
            quote.provider_market_id, quote.outcome_id,
        )):
            return False
        if (quote.adapter_id != binding.adapter_id
                or quote.provider_event_id != binding.provider_event_id
                or quote.provider_market_id != binding.provider_market_id
                or quote.outcome_id not in expected_provider_ids
                or quote.outcome_id in observed_outcome_ids):
            return False
        observed_outcome_ids.add(quote.outcome_id)
        if quote.state != QuoteState.OPEN:
            return False
        try:
            price = float(quote.price)
            received_at_monotonic = float(quote.received_at_monotonic)
        except (TypeError, ValueError, OverflowError):
            return False
        if not math.isfinite(price) or price <= 1.0:
            return False
        if not math.isfinite(received_at_monotonic):
            return False
        age = now_monotonic - received_at_monotonic
        if age < 0 or age > max_age_seconds:
            return False

    return observed_outcome_ids == expected_provider_ids
