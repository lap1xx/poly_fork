"""Polymarket/bookmaker candidate construction, independent of scanner lifecycle."""
from __future__ import annotations

from arb_invariants import is_pm_bookmaker_pair


def build_arb_candidates(
    market,
    live_bookmaker_quotes,
    slip,
    bank=0.0,
    rate=0.0,
    bookmaker_source="BetBoom",
    diagnostics=None,
    pm_diagnostics=None,
    market_slug=None,
    *,
    fetch_pm_fee_schedule,
    maybe_refresh_stale,
    best_ask,
    walk_book_with_fee,
    pm_age_sec,
    best_bid,
    pm_refresh_attempted,
    pm_book_diagnostic,
):
    """Build only valid opposite-outcome pairs for one bookmaker source.

    Runtime-dependent Polymarket book operations are injected by the legacy
    scanner facade, preserving its monkeypatch points and public call shape.
    """

    def note(reason):
        if diagnostics is not None:
            diagnostics[reason] = diagnostics.get(reason, 0) + 1

    source_label = str(bookmaker_source or "").strip()
    if not source_label or "polymarket" in source_label.casefold():
        note("invalid_bookmaker_source")
        return []

    pm_outcomes = market.get("o") or []
    pm_tokens = market.get("t") or []
    if len(pm_outcomes) != 2 or len(pm_tokens) != 2 or not live_bookmaker_quotes:
        note("invalid_market_or_no_quotes")
        return []

    fee_schedule = fetch_pm_fee_schedule(market.get("condition_id"))
    if fee_schedule is None:
        note("pm_fee_unavailable")
        return []
    pm_fee_rate = fee_schedule["rate"]
    pm_fee_exponent = fee_schedule["exponent"]

    usd_target = 50.0
    try:
        if bank and bank > 0:
            usd_target = max(20.0, min(200.0, bank * 0.5))
    except Exception:
        pass

    pm_legs = []
    for index, outcome in enumerate(pm_outcomes):
        try:
            maybe_refresh_stale(pm_tokens[index])
        except Exception:
            pass
        ask = best_ask(pm_tokens[index])
        if ask <= 0:
            note("pm_no_ask")
            if pm_diagnostics is not None:
                row = {
                    "market_slug": str(market_slug or market.get("slug") or ""),
                    "outcome": str(outcome),
                    "token_id": str(pm_tokens[index]),
                }
                row.update(pm_book_diagnostic(pm_tokens[index]))
                pm_diagnostics.append(row)
            return []
        depth_quote = walk_book_with_fee(
            pm_tokens[index], usd_target, pm_fee_rate, pm_fee_exponent
        )
        if not depth_quote:
            note("pm_depth_short")
            return []
        average_price = depth_quote["avg_price"]
        if average_price > ask * 1.05:
            note("pm_impact_rejected")
            return []
        book_age = pm_age_sec(pm_tokens[index])
        pm_legs.append({
            "source": "Polymarket",
            "outcome": outcome,
            "o": depth_quote["effective_multiplier"],
            "c": 0.0,
            "s": slip,
            "token": pm_tokens[index],
            "pm_fee_enabled": fee_schedule["fees_enabled"],
            "pm_fee_rate": pm_fee_rate,
            "pm_fee_exponent": pm_fee_exponent,
            "pm_fee_source": fee_schedule["source"],
            "pm_fee_estimated_usd": depth_quote["fee"],
            "pm_best_ask": ask,
            "pm_best_bid": best_bid(pm_tokens[index]),
            "pm_avg_ask": average_price,
            "pm_age": book_age,
            "pm_book_age_sec": book_age,
            "pm_target_usd": usd_target,
            "pm_target_shares": depth_quote["shares"],
            "pm_verified": pm_refresh_attempted(pm_tokens[index]),
        })

    bookmaker_legs = {}
    for outcome, info in live_bookmaker_quotes.items():
        try:
            odds = float(info.get("o") or 0)
        except Exception:
            continue
        if odds <= 1.0:
            continue
        bookmaker_legs[outcome] = {
            "source": source_label,
            "outcome": outcome,
            "o": odds,
            "c": float(info.get("c", 0.0)),
            "s": slip,
            "odds_age_sec": info.get("age_sec"),
        }

    pairs = []
    for index, pm_outcome in enumerate(pm_outcomes):
        opposite_outcome = pm_outcomes[1 - index]
        bookmaker_leg = bookmaker_legs.get(opposite_outcome)
        if bookmaker_leg:
            pair = [pm_legs[index], bookmaker_leg]
            if is_pm_bookmaker_pair(
                pair,
                expected_bookmaker_source=source_label,
                require_pm_token=True,
                require_prices=True,
            ):
                pairs.append(pair)
            else:
                note("invalid_candidate_pair")
    if not pairs:
        note("no_opposite_bk_market")
    return pairs
