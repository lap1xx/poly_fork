import unittest
from unittest.mock import Mock

from bookmaker_adapter_contract import (
    AdapterHealth,
    BinaryMarket,
    Binding,
    BookmakerAdapter,
    MarketOutcome,
    QuotePair,
    QuoteSnapshot,
    QuoteState,
    validate_binding,
    validate_quote_pair,
)


class SyntheticAdapter:
    """In-memory quote source; it has no network or account operations."""

    adapter_id = "synthetic-a"

    def __init__(self):
        self.started = False
        self.callback = None
        self.binding = Binding(
            adapter_id=self.adapter_id,
            provider_event_id="event-17",
            provider_market_id="market-winner",
            market_kind="match_winner",
            period="match",
            outcome_map=(("pm-a", "provider-home"), ("pm-b", "provider-away")),
            catalog_revision="rev-3",
            bound_at_epoch=100.0,
        )

    def start(self):
        self.started = True

    def stop(self):
        self.started = False

    def wait_ready(self, timeout):
        return self.started and timeout >= 0

    def bind(self, market):
        return self.binding

    def get_quote_pair(self, binding):
        if binding != self.binding:
            return None
        return QuotePair(
            adapter_id=self.adapter_id,
            provider_event_id="event-17",
            provider_market_id="market-winner",
            quotes=(
                QuoteSnapshot(
                    self.adapter_id, "event-17", "market-winner", "provider-home",
                    1.9, QuoteState.OPEN, 50.0, 1_700_000_050.0,
                    provider_timestamp=1_700_000_040.0,
                ),
                QuoteSnapshot(
                    self.adapter_id, "event-17", "market-winner", "provider-away",
                    2.1, QuoteState.OPEN, 50.2, 1_700_000_050.2,
                ),
            ),
        )

    def set_update_callback(self, callback):
        self.callback = callback

    def health(self):
        return AdapterHealth(
            adapter_id=self.adapter_id,
            connected=self.started,
            ready=self.started,
            catalog_complete=self.started,
            discovered_events=1,
            bound_events=1,
            fresh_pairs=1 if self.started else 0,
        )

    def emit_update(self, provider_event_id, received_at_monotonic):
        if self.callback is not None:
            self.callback(self.adapter_id, provider_event_id, received_at_monotonic)


class AdapterContractTests(unittest.TestCase):
    def setUp(self):
        self.market = BinaryMarket(
            market_key="pm-market-17",
            event_key="event-17",
            market_kind="match_winner",
            period="match",
            outcomes=(
                MarketOutcome("pm-a", "Alpha", "token-a"),
                MarketOutcome("pm-b", "Bravo", "token-b"),
            ),
        )
        self.adapter = SyntheticAdapter()

    def test_synthetic_adapter_implements_protocol_and_lifecycle(self):
        self.assertIsInstance(self.adapter, BookmakerAdapter)
        self.assertFalse(self.adapter.wait_ready(timeout=0.1))
        self.adapter.start()
        self.assertTrue(self.adapter.wait_ready(timeout=0.1))
        self.assertTrue(self.adapter.health().catalog_complete)
        self.adapter.stop()
        self.assertFalse(self.adapter.health().connected)

    def test_binding_and_both_fresh_quotes_from_one_adapter_are_accepted(self):
        binding = self.adapter.bind(self.market)
        pair = self.adapter.get_quote_pair(binding)
        self.assertTrue(validate_binding(self.market, binding))
        self.assertTrue(validate_quote_pair(
            self.market, binding, pair, now_monotonic=51.0, max_age_seconds=30.0,
        ))

    def test_match_type_period_and_ambiguous_binding_fail_closed(self):
        binding = self.adapter.binding
        wrong_period = BinaryMarket(
            market_key="pm-map-market", market_kind="map_winner", period="map-2",
            outcomes=self.market.outcomes,
        )
        self.assertFalse(validate_binding(wrong_period, binding))
        duplicate_provider_side = Binding(
            **{**binding.__dict__, "outcome_map": (("pm-a", "same"), ("pm-b", "same"))}
        )
        self.assertFalse(validate_binding(self.market, duplicate_provider_side))
        malformed_map = Binding(
            **{**binding.__dict__, "outcome_map": (None, ("pm-b", "provider-away"))}
        )
        self.assertFalse(validate_binding(self.market, malformed_map))
        self.assertFalse(validate_binding(self.market, Binding(
            **{**binding.__dict__, "match_status": "ambiguous"}
        )))

    def test_quote_pair_cannot_mix_sources_or_provider_markets(self):
        binding = self.adapter.binding
        pair = self.adapter.get_quote_pair(binding)
        wrong_source_quote = QuoteSnapshot(
            "synthetic-b", "event-17", "market-winner", "provider-away",
            2.1, QuoteState.OPEN, 50.2, 1_700_000_050.2,
        )
        mixed = QuotePair(
            pair.adapter_id, pair.provider_event_id, pair.provider_market_id,
            (pair.quotes[0], wrong_source_quote),
        )
        self.assertFalse(validate_quote_pair(
            self.market, binding, mixed, now_monotonic=51.0, max_age_seconds=30.0,
        ))
        wrong_market = QuotePair(
            pair.adapter_id, pair.provider_event_id, "other-market", pair.quotes,
        )
        self.assertFalse(validate_quote_pair(
            self.market, binding, wrong_market, now_monotonic=51.0, max_age_seconds=30.0,
        ))
        malformed_pair = QuotePair(
            pair.adapter_id, pair.provider_event_id, pair.provider_market_id,
            (pair.quotes[0], None),
        )
        self.assertFalse(validate_quote_pair(
            self.market, binding, malformed_pair,
            now_monotonic=51.0, max_age_seconds=30.0,
        ))

    def test_both_sides_must_be_open_finite_and_fresh_by_local_receipt(self):
        binding = self.adapter.binding
        good = self.adapter.get_quote_pair(binding)
        cases = {
            "one side stale": QuotePair(
                good.adapter_id, good.provider_event_id, good.provider_market_id,
                (good.quotes[0], QuoteSnapshot(
                    **{**good.quotes[1].__dict__, "received_at_monotonic": 10.0}
                )),
            ),
            "closed": QuotePair(
                good.adapter_id, good.provider_event_id, good.provider_market_id,
                (good.quotes[0], QuoteSnapshot(
                    **{**good.quotes[1].__dict__, "state": QuoteState.CLOSED}
                )),
            ),
            "non-finite price": QuotePair(
                good.adapter_id, good.provider_event_id, good.provider_market_id,
                (good.quotes[0], QuoteSnapshot(
                    **{**good.quotes[1].__dict__, "price": float("inf")}
                )),
            ),
            "non-numeric price": QuotePair(
                good.adapter_id, good.provider_event_id, good.provider_market_id,
                (good.quotes[0], QuoteSnapshot(
                    **{**good.quotes[1].__dict__, "price": "not-an-odds"}
                )),
            ),
            "future receipt": QuotePair(
                good.adapter_id, good.provider_event_id, good.provider_market_id,
                (good.quotes[0], QuoteSnapshot(
                    **{**good.quotes[1].__dict__, "received_at_monotonic": 52.0}
                )),
            ),
        }
        for label, pair in cases.items():
            with self.subTest(label=label):
                self.assertFalse(validate_quote_pair(
                    self.market, binding, pair,
                    now_monotonic=51.0, max_age_seconds=30.0,
                ))

    def test_update_callback_is_lightweight_and_health_is_non_sensitive(self):
        callback = Mock()
        self.adapter.set_update_callback(callback)
        self.adapter.emit_update("event-17", 51.25)
        callback.assert_called_once_with("synthetic-a", "event-17", 51.25)
        health_fields = set(self.adapter.health().__dataclass_fields__)
        for forbidden in ("username", "password", "account_id", "balance", "cookie"):
            self.assertNotIn(forbidden, health_fields)

    def test_protocol_exposes_no_account_coupon_or_bet_operations(self):
        names = set(dir(BookmakerAdapter))
        forbidden = {
            "login", "get_account", "get_balance", "create_coupon",
            "submit_coupon", "place_bet", "submit_bet", "withdraw",
        }
        self.assertFalse(forbidden.intersection(names))

    def test_binary_market_rejects_missing_period_or_non_binary_outcomes(self):
        with self.assertRaises(ValueError):
            BinaryMarket(
                market_key="market", market_kind="match_winner", period="",
                outcomes=self.market.outcomes,
            )
        with self.assertRaises(ValueError):
            BinaryMarket(
                market_key="market", market_kind="match_winner", period="match",
                outcomes=(self.market.outcomes[0],),
            )


if __name__ == "__main__":
    unittest.main()
