import sys
import unittest
import time

import price_history
import stats
import polyarb_v4


class CalculationTests(unittest.TestCase):
    def test_equalized_stakes_and_roi(self):
        result = polyarb_v4.compute([
            {"o": 2.1, "c": 0.0, "s": 0.0},
            {"o": 2.1, "c": 0.0, "s": 0.0},
        ], 1000)
        self.assertIsNotNone(result)
        self.assertAlmostEqual(result["roi"], 0.05, places=10)
        self.assertAlmostEqual(sum(result["stakes"]), 1000.0, places=8)
        self.assertAlmostEqual(result["payout"], 1050.0, places=8)
        self.assertAlmostEqual(result["profit"], 50.0, places=8)

    def test_invalid_inputs_fail_closed(self):
        self.assertIsNone(polyarb_v4.compute([], 1000))
        self.assertIsNone(polyarb_v4.compute([{"o": 1.0}], 1000))
        self.assertIsNone(polyarb_v4.compute([{"o": float("nan")}], 1000))
        self.assertEqual(polyarb_v4.eff_mult(2.0, 1.0, 0.0), 0.0)
        self.assertAlmostEqual(polyarb_v4.eff_mult(2.0, 0.1, 0.05), 1.81)

    def test_exchange_rate_uses_only_valid_values(self):
        self.assertEqual(polyarb_v4.resolve_usd_rub_rate(92.5, 85.0), 92.5)
        self.assertEqual(polyarb_v4.resolve_usd_rub_rate(None, 85.0), 85.0)
        self.assertEqual(polyarb_v4.resolve_usd_rub_rate(float("nan"), 85.0), 85.0)
        self.assertIsNone(polyarb_v4.resolve_usd_rub_rate(None, 0.0))

    def test_feed_discards_invalid_orderbook_levels(self):
        feed = polyarb_v4.Feed([])
        feed._a({
            "event_type": "book", "asset_id": "unit-test-invalid-book",
            "bids": [{"price": "nan", "size": "10"}, {"price": "0.4", "size": "inf"}],
            "asks": [{"price": "1.2", "size": "10"}],
        })
        with polyarb_v4.LOCK:
            book = polyarb_v4.BOOKS.pop("unit-test-invalid-book")
        self.assertEqual(book["bids"], [])
        self.assertEqual(book["asks"], [])

    def test_alert_gap_limits_changes_and_schedules_repeats(self):
        self.assertTrue(polyarb_v4.alert_due(True, None, 100.0, 30.0))
        self.assertFalse(polyarb_v4.alert_due(True, 90.0, 100.0, 30.0))
        self.assertTrue(polyarb_v4.alert_due(True, 60.0, 100.0, 30.0))
        self.assertFalse(polyarb_v4.alert_due(False, 90.0, 100.0, 30.0))
        self.assertTrue(polyarb_v4.alert_due(False, 60.0, 100.0, 30.0))
        self.assertFalse(polyarb_v4.alert_due(False, 100.0, 100.0, 0.0))

    def test_refine_uses_rub_usd_conversion_without_scaling_odds(self):
        token = "unit-test-token-rate"
        book = {"bids": [(0.49, 100.0)], "asks": [(0.50, 100.0)],
                "ts": time.time(), "last_change": time.time()}
        with polyarb_v4.LOCK:
            old = polyarb_v4.BOOKS.get(token)
            polyarb_v4.BOOKS[token] = book
        try:
            base = [
                {"source": "Polymarket", "token": token, "o": 2.0, "c": 0.0, "s": 0.0},
                {"source": "Book", "o": 2.2, "c": 0.0, "s": 0.0},
            ]
            outputs = []
            for rate in (85.0, 100.0):
                bets = [dict(leg) for leg in base]
                initial = polyarb_v4.compute(bets, 1000.0)
                refined = polyarb_v4.refine_stakes(bets, initial, rate)
                self.assertIsNotNone(refined)
                outputs.append(bets[0]["o"])
            expected = (1.0 / 0.50) / (1.0 + polyarb_v4.PM_FEE_PCT)
            self.assertAlmostEqual(outputs[0], expected, places=8)
            self.assertAlmostEqual(outputs[1], expected, places=8)
        finally:
            with polyarb_v4.LOCK:
                if old is None:
                    polyarb_v4.BOOKS.pop(token, None)
                else:
                    polyarb_v4.BOOKS[token] = old

    def test_refine_rejects_insufficient_book_depth(self):
        token = "unit-test-token-depth"
        book = {"bids": [(0.49, 0.001)], "asks": [(0.50, 0.001)],
                "ts": time.time(), "last_change": time.time()}
        with polyarb_v4.LOCK:
            old = polyarb_v4.BOOKS.get(token)
            polyarb_v4.BOOKS[token] = book
        try:
            bets = [
                {"source": "Polymarket", "token": token, "o": 2.0, "c": 0.0, "s": 0.0},
                {"source": "Book", "o": 2.2, "c": 0.0, "s": 0.0},
            ]
            initial = polyarb_v4.compute(bets, 1000.0)
            self.assertIsNone(polyarb_v4.refine_stakes(bets, initial, 85.0))
        finally:
            with polyarb_v4.LOCK:
                if old is None:
                    polyarb_v4.BOOKS.pop(token, None)
                else:
                    polyarb_v4.BOOKS[token] = old

    def test_feed_token_set_updates_without_starting_network(self):
        feed = polyarb_v4.Feed(["a", "b"])
        self.assertTrue(feed.update_tokens(["a", "b", "c"]))
        self.assertEqual(feed.ids, ["a", "b", "c"])
        self.assertFalse(feed.update_tokens(["c", "b", "a"]))

    def test_onewin_is_not_imported_or_enabled_by_default(self):
        from bk_router import BKRouter
        router = BKRouter(use_bb=False)
        self.assertFalse(router.use_onewin)
        self.assertNotIn("onewin_bridge", sys.modules)


class HistoryAndStatsTests(unittest.TestCase):
    def test_price_history_requires_actual_bid_and_ask(self):
        legacy = {"bets": [{"source": "Polymarket", "o": 2.0}]}
        self.assertEqual(price_history._extract_pm_price(legacy), (None, None))
        current = {"bets": [{"source": "Polymarket",
                              "pm_best_ask": 0.53, "pm_best_bid": 0.51}]}
        self.assertEqual(price_history._extract_pm_price(current), (0.53, 0.51))
        crossed = {"pm_best_ask": 0.50, "pm_best_bid": 0.51,
                   "bets": [{"source": "Polymarket"}]}
        self.assertEqual(price_history._extract_pm_price(crossed), (None, None))

    def test_filter_compares_mixed_iso_timezones_in_utc(self):
        rows = [{"slug": "match", "ts": "2026-11-19T03:00:00+03:00", "roi": 0.05}]
        got = stats.filter_rows(rows, since="2026-11-19T00:00:00Z", min_roi=4.9)
        self.assertEqual(len(got), 1)
        self.assertEqual(stats.filter_rows(rows, since="2026-11-19T00:01:00Z"), [])

    def test_csv_formula_cells_are_neutralized(self):
        self.assertEqual(stats._csv_safe("=1+1"), "'=1+1")
        self.assertEqual(stats._csv_safe("plain-slug"), "plain-slug")
        self.assertEqual(stats._csv_safe("-cmd"), "'-cmd")
        self.assertEqual(stats._csv_safe(12.5), 12.5)


if __name__ == "__main__":
    unittest.main()
