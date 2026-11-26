import asyncio
import math
import random
import unittest
from unittest.mock import patch

import pm_depth_quote as quote
import pm_market_data
import web_app


class PMDepthQuoteTests(unittest.TestCase):
    def test_multi_level_fee_and_two_outcome_payouts_use_entered_amounts(self):
        asks = [(0.4, 2.0), (0.5, 2.0), (0.6, 10.0)]
        result = quote.estimate_manual_position(asks, 1.3365, 1.5, 2.5, 0.05, 1.0)
        self.assertIsNotNone(result)
        pm = result["pm_quote"]
        self.assertTrue(pm["full"])
        self.assertAlmostEqual(pm["shares"], 3.0)
        self.assertAlmostEqual(pm["avg_price"], 1.3 / 3.0)
        self.assertAlmostEqual(pm["fee"], 0.0365)
        self.assertAlmostEqual(pm["total_cost"], 1.3365)
        self.assertAlmostEqual(result["bank"], 2.8365)
        self.assertAlmostEqual(result["pm_payout"], 3.0)
        self.assertAlmostEqual(result["bookmaker_payout"], 3.75)
        self.assertAlmostEqual(result["minimum_profit"], 0.1635)
        self.assertAlmostEqual(result["minimum_roi"], 0.1635 / 2.8365)

    def test_zero_fee_and_bookmaker_commission_are_applied(self):
        result = quote.estimate_manual_position([(0.5, 100)], 10, 10, 2.5, 0, 1, 0.1)
        self.assertAlmostEqual(result["pm_quote"]["fee"], 0)
        self.assertAlmostEqual(result["pm_payout"], 20)
        self.assertAlmostEqual(result["bookmaker_payout"], 23.5)
        self.assertAlmostEqual(result["minimum_roi"], 0)

    def test_pm_slippage_reserve_matches_scanner_multiplier(self):
        result = quote.estimate_manual_position([(0.5, 100)], 10, 10, 2.5, 0, 1,
                                                pm_slippage=0.1)
        self.assertAlmostEqual(result["pm_quote"]["shares"], 20)
        self.assertAlmostEqual(result["pm_payout"], 18)
        self.assertAlmostEqual(result["minimum_profit"], -2)
        self.assertAlmostEqual(result["minimum_roi"], -0.1)

    def test_insufficient_depth_never_becomes_a_complete_position(self):
        asks = [(0.5, 2)]
        partial = quote.quote_pm_asks(asks, 2, 0.05, 1)
        self.assertFalse(partial["full"])
        self.assertAlmostEqual(partial["total_cost"], 1.025)
        self.assertAlmostEqual(partial["unfilled_usd"], 0.975)
        self.assertAlmostEqual(partial["available_cash_usd"], 1.025)
        self.assertIsNone(quote.estimate_manual_position(asks, 2, 1, 2, 0.05, 1))

    def test_invalid_input_and_unsorted_book_fail_closed(self):
        bad_books = [[], [(0.5, float('nan'))], [(0.5, -1)],
                     [(0.5, 1), (0.4, 1)], [(float('nan'), 1)], [(0.5, float('inf'))]]
        for asks in bad_books:
            with self.subTest(asks=asks):
                self.assertIsNone(quote.quote_pm_asks(asks, 10, 0.05, 1))
        for budget, rate, exponent in [(0, .05, 1), (math.inf, .05, 1),
                                       (10, None, 1), (10, -1, 1), (10, 2, 1),
                                       (10, .05, 0), (10, .05, math.nan)]:
            with self.subTest(budget=budget, rate=rate, exponent=exponent):
                self.assertIsNone(quote.quote_pm_asks([(0.5, 100)], budget, rate, exponent))
        self.assertIsNone(quote.estimate_manual_position([(0.5, 100)], 10, 10, 1, .05, 1))
        self.assertIsNone(quote.estimate_manual_position([(0.5, 100)], 10, 10, math.inf, .05, 1))

    def test_budget_changes_reprice_depth_instead_of_scaling_an_old_snapshot(self):
        asks = [(0.4, 10), (0.8, 100)]
        small = quote.quote_pm_asks(asks, 4, 0, 1)
        large = quote.quote_pm_asks(asks, 12, 0, 1)
        self.assertAlmostEqual(small["shares"], 10)
        self.assertAlmostEqual(large["shares"], 20)
        self.assertAlmostEqual(large["avg_price"], 0.6)
        self.assertNotAlmostEqual(large["shares"], 12 * small["odds_eff"])

    def test_rest_api_identifies_token_and_reports_unfilled_depth(self):
        market = {'conditionId': 'cond', 'clobTokenIds': ['token-a', 'token-b'], 'outcomes': ['A', 'B']}
        book = {'bids': [], 'asks': [{'price': '0.5', 'size': '2'}]}
        schedule = {'fees_enabled': True, 'rate': .05, 'exponent': 1.0, 'source': 'clob_market_info'}
        with patch.object(web_app, '_http_get', side_effect=[[market], book, book]), \
             patch.object(web_app, 'fetch_fee_schedule', return_value=schedule):
            result = asyncio.run(web_app.api_pm_book('test-market', size_usd=2))
        self.assertTrue(result['ok'])
        self.assertEqual([item['token_id'] for item in result['books']], ['token-a', 'token-b'])
        partial = result['books'][0]['avg_for_size']
        self.assertFalse(partial['full'])
        self.assertAlmostEqual(partial['total_cost'], 1.025)
        self.assertAlmostEqual(partial['unfilled_usd'], .975)

    def test_random_quotes_agree_with_existing_api_and_scanner_math(self):
        rng = random.Random(261126)
        schedule = {"rate": .05, "exponent": 1.0}
        token = "test-depth-quote-parity"
        for _ in range(120):
            asks = [(p, rng.uniform(0.1, 80)) for p in
                    sorted(rng.sample([x / 100 for x in range(1, 99)], 6))]
            budget = rng.uniform(1, 150)
            actual = quote.quote_pm_asks(asks, budget, .05, 1)
            existing = web_app._walk_pm_asks_with_fee(asks, budget, schedule)
            self.assertEqual(actual["full"], existing["full"])
            for key in ("avg_price", "shares", "fee", "total_cost", "odds_eff"):
                self.assertAlmostEqual(actual[key], existing[key], places=9)
            with pm_market_data.LOCK:
                old = pm_market_data.BOOKS.get(token)
                pm_market_data.BOOKS[token] = {"asks": asks, "bids": [], "ts": 1000.0}
            try:
                with patch.object(pm_market_data.time, "time", return_value=1000.0):
                    scan = pm_market_data.walk_book_with_fee(token, budget, .05, 1)
                if actual["full"]:
                    self.assertIsNotNone(scan)
                    for key in ("avg_price", "shares", "fee", "total_cost"):
                        self.assertAlmostEqual(actual[key], scan[key], places=9)
                    self.assertAlmostEqual(actual["odds_eff"], scan["effective_multiplier"], places=9)
                else:
                    self.assertIsNone(scan)
            finally:
                with pm_market_data.LOCK:
                    if old is None:
                        pm_market_data.BOOKS.pop(token, None)
                    else:
                        pm_market_data.BOOKS[token] = old


if __name__ == "__main__":
    unittest.main()
