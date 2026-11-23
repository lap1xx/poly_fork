"""Normalization of the existing read-only scanner payload into Opportunity v1.

This module is deliberately not wired into the runtime API yet. It provides a
pure adapter for contract tests and deterministic replay before any transport
or UI migration.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import math
from typing import Any, Mapping

from arb_invariants import is_pm_bookmaker_pair

SCHEMA_VERSION = 1
DEFAULT_MAX_AGE_SECONDS = {
    "prediction_market": 30.0,
    "bookmaker": 30.0,
}
# The current scanner model applies a 4% PM fee when it builds effective odds.
# Keep this explicit until the scanner publishes the fee used per calculation.
DEFAULT_PM_FEE_FRACTION = 0.04


class OpportunityNormalizationError(ValueError):
    """Raised when a legacy scanner candidate cannot form a valid domain row."""


def _finite_number(value: Any, field: str, *, minimum: float | None = None) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise OpportunityNormalizationError(f"{field}: expected a number") from exc
    if not math.isfinite(number):
        raise OpportunityNormalizationError(f"{field}: number must be finite")
    if minimum is not None and number < minimum:
        raise OpportunityNormalizationError(f"{field}: must be >= {minimum}")
    return number


def _iso_utc(epoch_seconds: float) -> str:
    try:
        value = datetime.fromtimestamp(epoch_seconds, tz=timezone.utc)
    except (OverflowError, OSError, ValueError) as exc:
        raise OpportunityNormalizationError("timestamp: outside supported range") from exc
    return value.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _normalize_phase(value: Any) -> str:
    phase = str(value or "").strip().lower()
    return phase if phase in {"live", "prematch", "unknown"} else "unknown"


def _quote_age(bet: Mapping[str, Any], source_kind: str, arb: Mapping[str, Any]) -> float:
    if source_kind == "prediction_market":
        raw = bet.get("pm_book_age_sec")
        if raw is None:
            raw = bet.get("pm_age")
        if raw is None:
            raw = arb.get("pm_age")
    else:
        raw = bet.get("odds_age_sec", arb.get("bk_odds_age_sec"))
    if raw is None:
        raise OpportunityNormalizationError(f"{source_kind}: quote age is missing")
    return _finite_number(raw, f"{source_kind}.age_sec", minimum=0.0)


def _stable_id(slug: str, pm_leg: Mapping[str, Any], bookmaker_leg: Mapping[str, Any]) -> str:
    identity = {
        "slug": slug,
        "pm_token": pm_leg.get("token"),
        "pm_outcome": str(pm_leg.get("outcome") or "").strip().casefold(),
        "bookmaker": str(bookmaker_leg.get("source") or "").strip().casefold(),
        "bookmaker_outcome": str(bookmaker_leg.get("outcome") or "").strip().casefold(),
    }
    packed = json.dumps(identity, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return "opp_v1_" + hashlib.sha256(packed.encode("utf-8")).hexdigest()


def normalize_scanner_arb(
    slug: str,
    arb: Mapping[str, Any],
    *,
    now_epoch: float | None = None,
    max_age_seconds: Mapping[str, float] | None = None,
    pm_fee_fraction: float = DEFAULT_PM_FEE_FRACTION,
) -> dict[str, Any]:
    """Convert one current `polyarb_v4` candidate to the normalized domain model.

    The source scanner's `bets[].o` is an effective decimal multiplier used by
    its ROI calculation. For the domain record, PM price prefers the refined
    depth average, then the initial-probe average, then top ask; bookmaker price
    uses its decimal odds (`o`). Stake values come from the aligned `stakes`
    array, not the legacy `bets[].s` field (which is slippage).
    """
    if not isinstance(slug, str) or not slug.strip() or len(slug) > 512:
        raise OpportunityNormalizationError("slug: invalid")
    if not isinstance(arb, Mapping):
        raise OpportunityNormalizationError("candidate: expected an object")

    candidate_slug = arb.get("slug")
    if candidate_slug and str(candidate_slug) != slug:
        raise OpportunityNormalizationError("slug: key and candidate slug differ")

    bets = arb.get("bets")
    stakes = arb.get("stakes")
    if not isinstance(bets, list) or len(bets) != 2:
        raise OpportunityNormalizationError("bets: exactly two legs are required")
    if not isinstance(stakes, list) or len(stakes) != 2:
        raise OpportunityNormalizationError("stakes: exactly two aligned values are required")
    if not is_pm_bookmaker_pair(
        bets,
        require_pm_token=True,
        require_prices=True,
    ):
        raise OpportunityNormalizationError("bets: scanner PM↔bookmaker invariant failed")

    pm_index = next(i for i, leg in enumerate(bets)
                    if str(leg.get("source", "")).strip().casefold() == "polymarket")
    bookmaker_index = 1 - pm_index
    raw_pm = bets[pm_index]
    raw_bookmaker = bets[bookmaker_index]
    declared_bookmaker = str(arb.get("bk_source") or "").strip().casefold()
    actual_bookmaker = str(raw_bookmaker.get("source") or "").strip().casefold()
    if declared_bookmaker and declared_bookmaker != actual_bookmaker:
        raise OpportunityNormalizationError(
            "bk_source: metadata does not match the bookmaker leg"
        )
    pm_outcome = str(raw_pm.get("outcome") or "").strip()
    bookmaker_outcome = str(raw_bookmaker.get("outcome") or "").strip()
    if not pm_outcome or not bookmaker_outcome:
        raise OpportunityNormalizationError("outcomes: both legs require a label")
    if pm_outcome.casefold() == bookmaker_outcome.casefold():
        raise OpportunityNormalizationError("outcomes: legs must be opposite")

    token_id = str(raw_pm.get("token") or "").strip()
    if not token_id:
        raise OpportunityNormalizationError("prediction_market.token_id: missing")

    generated_epoch = _finite_number(arb.get("ts"), "ts", minimum=0.0)
    now_value = generated_epoch if now_epoch is None else _finite_number(
        now_epoch, "now_epoch", minimum=0.0
    )
    if generated_epoch > now_value + 60.0:
        raise OpportunityNormalizationError("ts: candidate timestamp is too far in the future")
    ages = dict(DEFAULT_MAX_AGE_SECONDS)
    if max_age_seconds is not None:
        for key in ages:
            if key in max_age_seconds:
                ages[key] = _finite_number(
                    max_age_seconds[key], f"max_age_seconds.{key}", minimum=0.0
                )

    currency = str(arb.get("currency") or "USDT").upper()
    if currency not in {"USDT", "USD", "RUB"}:
        raise OpportunityNormalizationError("currency: unsupported")

    normalized_legs = []
    leg_sources = ((pm_index, "prediction_market"),
                   (bookmaker_index, "bookmaker"))
    stale = False
    for index, source_kind in leg_sources:
        raw = bets[index]
        age_at_snapshot = _quote_age(raw, source_kind, arb)
        age_now = age_at_snapshot + max(0.0, now_value - generated_epoch)
        if age_now > ages[source_kind]:
            stale = True
        received_epoch = generated_epoch - age_at_snapshot
        outcome = str(raw.get("outcome") or "").strip()
        source = str(raw.get("source") or "").strip()
        stake = _finite_number(stakes[index], f"stakes[{index}]", minimum=0.0)
        calculation_input_multiplier = _finite_number(
            raw.get("o"), f"bets[{index}].o", minimum=1.0
        )
        commission_fraction = _finite_number(
            raw.get("c", 0.0), f"bets[{index}].c", minimum=0.0
        )
        slippage_fraction = _finite_number(
            raw.get("s", 0.0), f"bets[{index}].s", minimum=0.0
        )
        if calculation_input_multiplier <= 1.0:
            raise OpportunityNormalizationError(
                f"bets[{index}].o: calculation multiplier must be > 1"
            )
        if commission_fraction >= 1.0:
            raise OpportunityNormalizationError(f"bets[{index}].c: must be < 1")
        if slippage_fraction >= 1.0:
            raise OpportunityNormalizationError(f"bets[{index}].s: must be < 1")
        calculation_inputs = {
            "calculation_input_multiplier": calculation_input_multiplier,
            "commission_fraction": commission_fraction,
            "slippage_fraction": slippage_fraction,
        }
        if source_kind == "prediction_market":
            refined_price = raw.get("pm_refined_avg_ask")
            refined_shares = raw.get("pm_refined_shares")
            try:
                has_refined_depth = (
                    float(refined_price) > 0.0 and float(refined_shares) > 0.0
                )
            except (TypeError, ValueError, OverflowError):
                has_refined_depth = False
            if has_refined_depth:
                price_value = refined_price
                price_basis = "depth_average"
                price_size_shares = _finite_number(
                    refined_shares, "prediction_market.refined_shares", minimum=0.0
                )
            elif raw.get("pm_avg_ask") is not None:
                price_value = raw.get("pm_avg_ask")
                price_basis = "depth_average"
                raw_size = raw.get("pm_target_shares")
                price_size_shares = (
                    _finite_number(
                        raw_size, "prediction_market.target_shares", minimum=0.0
                    ) if raw_size is not None else None
                )
            else:
                price_value = raw.get("pm_best_ask")
                price_basis = "top_of_book"
                price_size_shares = None
            price = _finite_number(price_value, "prediction_market.ask", minimum=0.0)
            if not 0.0 < price <= 1.0:
                raise OpportunityNormalizationError(
                    "prediction_market.ask: must be greater than 0 and <= 1"
                )
            leg = {
                "source": source,
                "source_kind": source_kind,
                "outcome": outcome,
                "price": price,
                "price_unit": "probability",
                "price_basis": price_basis,
                "price_size_shares": price_size_shares,
                "stake": stake,
                "currency": currency,
                "observed_at": _iso_utc(received_epoch),
                "age_ms": round(age_now * 1000.0, 3),
                "timestamp_basis": "received_at",
                "provider_observed_at": None,
                "token_id": token_id,
                **calculation_inputs,
            }
        else:
            price = _finite_number(raw.get("o"), "bookmaker.odds", minimum=1.0)
            if price <= 1.0:
                raise OpportunityNormalizationError("bookmaker.odds: must be > 1")
            leg = {
                "source": source,
                "source_kind": source_kind,
                "outcome": outcome,
                "price": price,
                "price_unit": "decimal_odds",
                "stake": stake,
                "currency": currency,
                "observed_at": _iso_utc(received_epoch),
                "age_ms": round(age_now * 1000.0, 3),
                "timestamp_basis": "received_at",
                "provider_observed_at": None,
                **calculation_inputs,
            }
        event_url_key = (
            "pm_event_url" if source_kind == "prediction_market" else "bk_event_url"
        )
        event_url = arb.get(event_url_key)
        if isinstance(event_url, str) and event_url.strip():
            leg["event_url"] = event_url.strip()
        normalized_legs.append(leg)

    roi = _finite_number(arb.get("roi"), "roi")
    total_stake = sum(leg["stake"] for leg in normalized_legs)
    profit = _finite_number(arb.get("profit"), "profit")
    fee = _finite_number(arb.get("pm_fee_fraction", pm_fee_fraction),
                         "pm_fee_fraction", minimum=0.0)
    if fee > 1.0:
        raise OpportunityNormalizationError("pm_fee_fraction: must be <= 1")

    confidence = None
    binding = arb.get("bk_binding")
    if isinstance(binding, Mapping) and binding.get("confidence") is not None:
        confidence = _finite_number(binding.get("confidence"), "mapping.confidence")
        if not 0.0 <= confidence <= 1.0:
            raise OpportunityNormalizationError("mapping.confidence: must be between 0 and 1")

    market_question = str(arb.get("q") or arb.get("title") or "").strip()
    if not market_question:
        raise OpportunityNormalizationError("market.question: missing")
    sport = str(arb.get("sport") or "unknown").strip() or "unknown"
    market_type = str(arb.get("market_type") or "unknown").strip() or "unknown"
    phase = _normalize_phase(arb.get("phase"))
    bookmaker_source = str(raw_bookmaker.get("source") or "").strip()
    opportunity_status = "stale" if stale else "active"

    return {
        "schema_version": SCHEMA_VERSION,
        "id": _stable_id(slug, raw_pm, raw_bookmaker),
        "status": opportunity_status,
        "observed_at": max(leg["observed_at"] for leg in normalized_legs),
        "computed_at": _iso_utc(generated_epoch),
        "market": {
            "slug": slug,
            "question": market_question,
            "sport": sport,
            "market_type": market_type,
            "phase": phase,
            "starts_at": None,
        },
        "mapping": {
            "status": "accepted",
            "method": "automated",
            "confidence": confidence,
            "reason_codes": ["scanner_candidate_validated"],
        },
        "legs": normalized_legs,
        "calculation": {
            "roi_fraction": roi,
            "total_stake": total_stake,
            "projected_profit": profit,
            "currency": currency,
            "fee_fraction": fee,
            "model_version": "polyarb_v4-effective-odds",
        },
        "diagnostics": [],
    }


def normalize_scanner_snapshot(
    payload: Mapping[str, Any],
    *,
    now_epoch: float | None = None,
    max_age_seconds: Mapping[str, float] | None = None,
) -> dict[str, Any]:
    """Normalize a `scanner.snapshot` v1 envelope without mutating the input.

    Per-candidate failures are reported by slug so replay tests and a future
    migration can observe rejected rows rather than silently dropping them.
    """
    if not isinstance(payload, Mapping):
        raise OpportunityNormalizationError("snapshot: expected an object")
    if (payload.get("type") != "scanner.snapshot"
            or isinstance(payload.get("schema_version"), bool)
            or payload.get("schema_version") != 1):
        raise OpportunityNormalizationError("snapshot: unsupported transport envelope")
    if payload.get("scanner_id") != "polyarb_v4":
        raise OpportunityNormalizationError("snapshot: unsupported scanner_id")
    session_id = payload.get("session_id")
    seq = payload.get("seq")
    if not isinstance(session_id, str) or not session_id or len(session_id) > 80:
        raise OpportunityNormalizationError("snapshot: invalid session_id")
    if isinstance(seq, bool) or not isinstance(seq, int) or seq < 1:
        raise OpportunityNormalizationError("snapshot: invalid seq")
    sent_at = _finite_number(payload.get("sent_at"), "sent_at", minimum=0.0)
    arbs = payload.get("arbs")
    if not isinstance(arbs, Mapping):
        raise OpportunityNormalizationError("snapshot.arbs: expected an object")

    opportunities = []
    rejected = []
    for slug, candidate in arbs.items():
        try:
            opportunities.append(normalize_scanner_arb(
                slug,
                candidate,
                now_epoch=now_epoch if now_epoch is not None else sent_at,
                max_age_seconds=max_age_seconds,
            ))
        except OpportunityNormalizationError as exc:
            rejected.append({"slug": str(slug)[:512], "reason": str(exc)})
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": _iso_utc(sent_at),
        "opportunities": opportunities,
        "rejected": rejected,
    }
