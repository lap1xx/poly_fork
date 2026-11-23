import json
from pathlib import Path
import unittest
from jsonschema import Draft202012Validator, FormatChecker, ValidationError

from opportunity import (
    OpportunityNormalizationError,
    normalize_scanner_snapshot,
)

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "quote_quality_replay.json"
SCHEMA_PATH = (
    Path(__file__).parents[1]
    / "docs"
    / "architecture"
    / "opportunity-v1.schema.json"
)


class OpportunityContractReplayTests(unittest.TestCase):
    """Deterministic, offline adapter tests based on captured read-only quotes."""

    @classmethod
    def setUpClass(cls):
        cls.fixture = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
        cls.schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(cls.schema)
        cls.validator = Draft202012Validator(
            cls.schema, format_checker=FormatChecker()
        )

    def make_snapshot(self, *, pm_age_delta=0.0, bookmaker_age_delta=0.0):
        fixture = self.fixture
        market_slug = fixture["market_slug"]
        captured = fixture["captured_at_epoch"]
        pm_row = fixture["polymarket"]["books"][0]
        bookmaker_row = fixture["onewin"]["quotes"][1]
        pm_age = captured - pm_row["local_received_at_epoch"] + pm_age_delta
        bookmaker_age = (
            captured - bookmaker_row["local_received_at_epoch"]
            + bookmaker_age_delta
        )
        ask = pm_row["asks"][0][0]
        odds = bookmaker_row["odds"]
        return {
            "type": "scanner.snapshot",
            "schema_version": 1,
            "scanner_id": "polyarb_v4",
            "session_id": "offline-replay-session",
            "seq": 1,
            "sent_at": captured,
            "arbs": {
                market_slug: {
                    "slug": market_slug,
                    "q": fixture["question"],
                    "sport": "cs2",
                    "market_type": "winner",
                    "phase": "prematch",
                    "currency": "USDT",
                    "roi": 0.04,
                    "bank": 40.0,
                    "stakes": [20.0, 20.0],
                    "profit": 1.6,
                    "ts": captured,
                    "bk_source": "1win",
                    "bk_odds_age_sec": bookmaker_age,
                    "bk_binding": {"confidence": 0.98},
                    "bets": [
                        {
                            "source": "Polymarket",
                            "outcome": pm_row["outcome"],
                            "token": pm_row["token_id"],
                            "o": 1.0 / (ask * 1.04),
                            "c": 0.0,
                            "s": 0.0,
                            "pm_best_ask": ask,
                            "pm_avg_ask": ask,
                            "pm_book_age_sec": pm_age,
                        },
                        {
                            "source": "1win",
                            "outcome": bookmaker_row["outcome"],
                            "o": odds,
                            "c": 0.0,
                            "s": 0.0,
                            "odds_age_sec": bookmaker_age,
                        },
                    ],
                }
            },
            "scan_status": {"state": "running"},
        }

    def test_captured_snapshot_normalizes_to_two_typed_legs(self):
        snapshot = self.make_snapshot()
        result = normalize_scanner_snapshot(snapshot)
        self.assertEqual(result["schema_version"], 1)
        self.assertEqual(result["rejected"], [])
        self.assertEqual(len(result["opportunities"]), 1)
        opportunity = result["opportunities"][0]
        self.assertEqual(opportunity["status"], "active")
        self.assertEqual(opportunity["market"]["slug"], self.fixture["market_slug"])
        self.assertEqual(opportunity["calculation"]["roi_fraction"], 0.04)
        self.assertEqual(opportunity["calculation"]["currency"], "USDT")
        self.assertEqual(
            [leg["source_kind"] for leg in opportunity["legs"]],
            ["prediction_market", "bookmaker"],
        )
        self.assertAlmostEqual(
            opportunity["legs"][0]["price"],
            self.fixture["polymarket"]["books"][0]["asks"][0][0],
        )
        self.assertAlmostEqual(
            opportunity["legs"][1]["price"],
            self.fixture["onewin"]["quotes"][1]["odds"],
        )
        self.assertEqual(opportunity["legs"][1]["timestamp_basis"], "received_at")
        self.assertIsNone(opportunity["legs"][1]["provider_observed_at"])
        self.assertAlmostEqual(
            opportunity["legs"][1]["age_ms"],
            (self.fixture["captured_at_epoch"]
             - self.fixture["onewin"]["quotes"][1]["local_received_at_epoch"])
            * 1000,
            places=2,
        )
        self.assertEqual(len(opportunity["id"]), len("opp_v1_") + 64)

    def test_calculation_uses_aligned_stakes_and_depth_average_not_account_bank_or_best_ask(self):
        snapshot = self.make_snapshot()
        candidate = snapshot["arbs"][self.fixture["market_slug"]]
        candidate["bank"] = 900.0
        candidate["total_bank"] = 1000.0
        pm_leg = candidate["bets"][0]
        pm_leg["pm_best_ask"] = 0.83
        pm_leg["pm_avg_ask"] = 0.84
        pm_leg["o"] = 1.0 / (0.84 * 1.04)

        result = normalize_scanner_snapshot(snapshot)
        self.assertEqual(result["rejected"], [])
        opportunity = result["opportunities"][0]
        self.assertEqual(
            [leg["stake"] for leg in opportunity["legs"]],
            candidate["stakes"],
        )
        self.assertEqual(opportunity["calculation"]["total_stake"], 40.0)
        self.assertEqual(opportunity["calculation"]["projected_profit"], 1.6)
        self.assertEqual(opportunity["calculation"]["roi_fraction"], 0.04)
        self.assertEqual(opportunity["legs"][0]["price"], 0.84)
        self.assertEqual(opportunity["legs"][0]["price_unit"], "probability")

    def test_calculation_inputs_preserve_exact_legacy_eff_mult_arguments(self):
        snapshot = self.make_snapshot()
        candidate = snapshot["arbs"][self.fixture["market_slug"]]
        pm, bookmaker = candidate["bets"]
        pm["c"], pm["s"] = 0.02, 0.01
        bookmaker["c"], bookmaker["s"] = 0.03, 0.04

        result = normalize_scanner_snapshot(snapshot)
        self.assertEqual(result["rejected"], [])
        legs = result["opportunities"][0]["legs"]
        for index, source in enumerate(candidate["bets"]):
            self.assertEqual(legs[index]["calculation_input_multiplier"], source["o"])
            self.assertEqual(legs[index]["commission_fraction"], source["c"])
            self.assertEqual(legs[index]["slippage_fraction"], source["s"])
        self.validator.validate(result["opportunities"][0])

    def test_invalid_calculation_input_fraction_is_rejected(self):
        for field in ("c", "s"):
            with self.subTest(field=field):
                snapshot = self.make_snapshot()
                snapshot["arbs"][self.fixture["market_slug"]]["bets"][0][field] = 1.0
                result = normalize_scanner_snapshot(snapshot)
                self.assertEqual(result["opportunities"], [])
                self.assertIn("must be < 1", result["rejected"][0]["reason"])

    def test_refined_pm_quote_and_provider_links_are_attached_to_their_legs(self):
        snapshot = self.make_snapshot()
        candidate = snapshot["arbs"][self.fixture["market_slug"]]
        candidate["pm_event_url"] = "https://polymarket.com/event/captured-market"
        candidate["bk_event_url"] = "https://example-bookmaker.test/event/123"
        pm_leg = candidate["bets"][0]
        pm_leg["pm_refined_avg_ask"] = 0.845
        pm_leg["pm_refined_shares"] = 46.0
        pm_leg["pm_refined_stake_usd"] = 38.18

        result = normalize_scanner_snapshot(snapshot)
        self.assertEqual(result["rejected"], [])
        pm, bookmaker = result["opportunities"][0]["legs"]
        self.assertEqual(pm["price"], 0.845)
        self.assertEqual(pm["price_basis"], "depth_average")
        self.assertEqual(pm["price_size_shares"], 46.0)
        self.assertEqual(
            pm["event_url"], "https://polymarket.com/event/captured-market"
        )
        self.assertEqual(
            bookmaker["event_url"], "https://example-bookmaker.test/event/123"
        )
        self.validator.validate(result["opportunities"][0])

    def test_stale_quote_is_explicitly_marked_stale(self):
        result = normalize_scanner_snapshot(
            self.make_snapshot(bookmaker_age_delta=31.0)
        )
        self.assertEqual(result["rejected"], [])
        self.assertEqual(result["opportunities"][0]["status"], "stale")

    def test_candidate_with_missing_quote_age_is_rejected_with_reason(self):
        payload = self.make_snapshot()
        pm_leg = payload["arbs"][self.fixture["market_slug"]]["bets"][0]
        pm_leg.pop("pm_book_age_sec", None)
        pm_leg.pop("pm_age", None)
        payload["arbs"][self.fixture["market_slug"]].pop("pm_age", None)
        result = normalize_scanner_snapshot(payload)
        self.assertEqual(result["opportunities"], [])
        self.assertEqual(len(result["rejected"]), 1)
        self.assertIn("quote age is missing", result["rejected"][0]["reason"])

    def test_pm_quote_age_prefers_non_null_leg_alias_before_root_fallback(self):
        payload = self.make_snapshot()
        candidate = payload["arbs"][self.fixture["market_slug"]]
        candidate["pm_age"] = 15.0
        pm_leg = candidate["bets"][0]
        pm_leg["pm_book_age_sec"] = None
        pm_leg["pm_age"] = 12.0

        result = normalize_scanner_snapshot(payload)
        self.assertEqual(result["rejected"], [])
        self.assertAlmostEqual(
            result["opportunities"][0]["legs"][0]["age_ms"], 12_000.0, places=2
        )

    def test_bookmaker_to_bookmaker_or_same_outcome_pair_is_rejected(self):
        payload = self.make_snapshot()
        candidate = payload["arbs"][self.fixture["market_slug"]]
        candidate["bets"][0]["source"] = "BetBoom"
        result = normalize_scanner_snapshot(payload)
        self.assertEqual(result["opportunities"], [])
        self.assertTrue(result["rejected"])

    def test_invalid_probability_price_is_rejected(self):
        payload = self.make_snapshot()
        candidate = payload["arbs"][self.fixture["market_slug"]]
        candidate["bets"][0]["pm_avg_ask"] = 1.01
        result = normalize_scanner_snapshot(payload)
        self.assertEqual(result["opportunities"], [])
        self.assertIn("<= 1", result["rejected"][0]["reason"])

    def test_bookmaker_source_metadata_must_match_the_leg(self):
        payload = self.make_snapshot()
        candidate = payload["arbs"][self.fixture["market_slug"]]
        candidate["bk_source"] = "BetBoom"
        result = normalize_scanner_snapshot(payload)
        self.assertEqual(result["opportunities"], [])
        self.assertIn("bk_source", result["rejected"][0]["reason"])

    def test_unsupported_transport_envelope_raises(self):
        payload = self.make_snapshot()
        payload["schema_version"] = 99
        with self.assertRaisesRegex(
            OpportunityNormalizationError, "unsupported transport envelope"
        ):
            normalize_scanner_snapshot(payload)

    def test_contract_schema_is_present_and_has_expected_version(self):
        self.assertEqual(
            self.schema["$schema"],
            "https://json-schema.org/draft/2020-12/schema",
        )
        self.assertEqual(self.schema["title"], "PolyFork Opportunity v1")
        self.assertIn("legs", self.schema["required"])

    def test_normalized_replay_record_validates_against_json_schema(self):
        result = normalize_scanner_snapshot(self.make_snapshot())
        self.assertEqual(result["rejected"], [])
        self.validator.validate(result["opportunities"][0])

    def test_json_schema_rejects_probability_above_one(self):
        result = normalize_scanner_snapshot(self.make_snapshot())
        opportunity = result["opportunities"][0]
        opportunity["legs"][0]["price"] = 1.01
        with self.assertRaises(ValidationError):
            self.validator.validate(opportunity)


if __name__ == "__main__":
    unittest.main()
