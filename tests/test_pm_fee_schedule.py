import asyncio
import unittest
from unittest.mock import Mock, patch

import polyarb_v4
import pm_fees
import web_app


class PMFeeScheduleTests(unittest.TestCase):
    def setUp(self):
        pm_fees.clear_fee_cache()

    def tearDown(self):
        pm_fees.clear_fee_cache()

    def test_normalizes_enabled_market_fee_curve(self):
        self.assertEqual(
            pm_fees.normalize_fee_schedule({
                "feesEnabled": True,
                "feeSchedule": {"rate": "0.05", "exponent": "1", "takerOnly": True},
            }),
            {"fees_enabled": True, "rate": 0.05, "exponent": 1.0,
             "source": "clob_market_info"},
        )

    def test_fee_free_market_accepts_zero_curve(self):
        self.assertEqual(
            pm_fees.normalize_fee_schedule({
                "feesEnabled": False,
                "feeSchedule": {"rate": 0, "exponent": 1},
            })["rate"],
            0.0,
        )
        self.assertEqual(
            pm_fees.normalize_fee_schedule({"feesEnabled": False})[
                "fees_enabled"
            ],
            False,
        )

    def test_invalid_or_conflicting_schedule_is_rejected(self):
        invalid = [
            {},
            {"feesEnabled": True, "feeSchedule": {"rate": "bad", "exponent": 1}},
            {"feesEnabled": True, "feeSchedule": {"rate": -0.1, "exponent": 1}},
            {"feesEnabled": True, "feeSchedule": {"rate": 0.05, "exponent": 0}},
            {"feesEnabled": False, "feeSchedule": {"rate": 0.05, "exponent": 1}},
        ]
        for payload in invalid:
            with self.subTest(payload=payload):
                self.assertIsNone(pm_fees.normalize_fee_schedule(payload))

    def test_fetches_market_info_and_uses_ttl_cache(self):
        response = Mock()
        response.json.return_value = {
            "feesEnabled": True,
            "feeSchedule": {"rate": 0.07, "exponent": 2},
        }
        with patch.object(pm_fees.requests, "get", return_value=response) as get:
            first = polyarb_v4.fetch_pm_fee_schedule("condition-id", now=100.0)
            second = polyarb_v4.fetch_pm_fee_schedule("condition-id", now=200.0)
        self.assertEqual(first["rate"], 0.07)
        self.assertEqual(second["exponent"], 2.0)
        get.assert_called_once_with(
            polyarb_v4.CLOB + "/markets/condition-id",
            timeout=pm_fees.PM_FEE_API_TIMEOUT,
        )
        response.raise_for_status.assert_called_once()

    def test_expired_cache_refetches_and_api_failure_is_not_cached(self):
        first_response = Mock()
        first_response.json.return_value = {
            "feesEnabled": True,
            "feeSchedule": {"rate": 0.05, "exponent": 1},
        }
        second_response = Mock()
        second_response.json.return_value = {
            "feesEnabled": True,
            "feeSchedule": {"rate": 0.04, "exponent": 1},
        }
        with patch.object(
            pm_fees.requests, "get",
            side_effect=[OSError("offline"), first_response, second_response],
        ) as get:
            self.assertIsNone(polyarb_v4.fetch_pm_fee_schedule("condition-id", now=0))
            self.assertEqual(
                polyarb_v4.fetch_pm_fee_schedule("condition-id", now=10)["rate"],
                0.05,
            )
            self.assertEqual(
                polyarb_v4.fetch_pm_fee_schedule(
                    "condition-id", now=10 + pm_fees.PM_FEE_CACHE_TTL_SEC + 1
                )["rate"],
                0.04,
            )
        self.assertEqual(get.call_count, 3)

    def test_fee_curve_is_applied_per_price_level_and_cash_budget(self):
        token = "unit-test-fee-walk"
        now = polyarb_v4.time.time()
        with polyarb_v4.LOCK:
            previous = polyarb_v4.BOOKS.get(token)
            polyarb_v4.BOOKS[token] = {
                "asks": [(0.50, 100.0)], "bids": [], "ts": now,
            }
        try:
            result = polyarb_v4.walk_book_with_fee(token, 1.025, 0.05, 1.0)
            self.assertAlmostEqual(result["shares"], 2.0)
            self.assertAlmostEqual(result["fee"], 0.025)
            self.assertAlmostEqual(result["total_cost"], 1.025)
            self.assertAlmostEqual(result["effective_multiplier"], 2 / 1.025)
            self.assertAlmostEqual(
                polyarb_v4.pm_fee_per_share(0.5, 0.25, 2.0), 0.015625
            )
        finally:
            with polyarb_v4.LOCK:
                if previous is None:
                    polyarb_v4.BOOKS.pop(token, None)
                else:
                    polyarb_v4.BOOKS[token] = previous

    def test_candidate_fails_closed_without_market_fee_schedule(self):
        diagnostics = {}
        with patch.object(polyarb_v4, "fetch_pm_fee_schedule", return_value=None):
            result = polyarb_v4.build_arb_candidates(
                {"condition_id": "unknown-market", "o": ["A", "B"],
                 "t": ["token-a", "token-b"]},
                {"A": {"o": 2.0}}, 0.0, diagnostics=diagnostics,
            )
        self.assertEqual(result, [])
        self.assertEqual(diagnostics.get("pm_fee_unavailable"), 1)

    def test_web_book_endpoint_uses_schedule_and_fee_aware_cash_depth(self):
        market = {
            "conditionId": "condition-id",
            "clobTokenIds": '["token-a", "token-b"]',
            "outcomes": '["A", "B"]',
        }
        book = {"bids": [], "asks": [{"price": "0.5", "size": "100"}]}
        schedule = {
            "fees_enabled": True, "rate": 0.05, "exponent": 1.0,
            "source": "clob_market_info",
        }
        with patch.object(web_app, "_http_get", side_effect=[[market], book, book]), \
             patch.object(web_app, "fetch_fee_schedule", return_value=schedule):
            response = asyncio.run(web_app.api_pm_book("test-market", size_usd=1.025))
        self.assertTrue(response["ok"])
        self.assertEqual(response["fee_schedule"], schedule)
        quote = response["books"][0]["avg_for_size"]
        self.assertAlmostEqual(quote["shares"], 2.0)
        self.assertAlmostEqual(quote["fee_estimate"], 0.025)
        self.assertAlmostEqual(quote["odds_eff"], 2 / 1.025, places=4)

    def test_web_book_endpoint_fails_closed_without_schedule(self):
        market = {
            "conditionId": "condition-id",
            "clobTokenIds": '["token-a", "token-b"]',
            "outcomes": '["A", "B"]',
        }
        with patch.object(web_app, "_http_get", return_value=[market]), \
             patch.object(web_app, "fetch_fee_schedule", return_value=None):
            response = asyncio.run(web_app.api_pm_book("test-market", size_usd=10))
        self.assertFalse(response["ok"])
        self.assertEqual(response["err"], "fee_schedule_unavailable")


if __name__ == "__main__":
    unittest.main()
