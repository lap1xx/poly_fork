import json
import sys
import unittest
import time
from unittest.mock import patch

import price_history
import stats
import polyarb_v4
import bot_live
from event_phase import classify_event_phase


class EventPhaseTests(unittest.TestCase):
    def setUp(self):
        from datetime import datetime, timezone
        self.now = datetime(2030, 1, 1, 12, 0, tzinfo=timezone.utc)

    def test_explicit_live_signal_is_live(self):
        self.assertEqual(classify_event_phase({"live": True}, now=self.now), "live")
        self.assertEqual(classify_event_phase({}, {"status": "in-play"}, now=self.now), "live")

    def test_future_start_and_scheduled_status_are_prematch(self):
        self.assertEqual(
            classify_event_phase({"start": "2030-01-01T13:00:00Z"}, now=self.now),
            "prematch",
        )
        self.assertEqual(
            classify_event_phase({"status": "scheduled"}, now=self.now),
            "prematch",
        )

    def test_past_start_missing_state_and_map_slug_are_unknown(self):
        self.assertEqual(
            classify_event_phase({"start": "2030-01-01T11:00:00Z"}, now=self.now),
            "unknown",
        )
        self.assertEqual(classify_event_phase({"slug": "cs2-alpha-beta-map3"}, now=self.now), "unknown")
        self.assertEqual(classify_event_phase({}, now=self.now), "unknown")

    def test_terminal_or_inactive_market_is_not_prematch(self):
        self.assertEqual(
            classify_event_phase({"closed": "true", "start": "2030-01-02T00:00:00Z"}, now=self.now),
            "unknown",
        )
        self.assertEqual(classify_event_phase({"active": False}, now=self.now), "unknown")

    def test_conflicting_provider_signals_fail_closed(self):
        self.assertEqual(
            classify_event_phase({"live": True}, {"live": False}, now=self.now),
            "unknown",
        )
        self.assertEqual(
            classify_event_phase({"status": "in-play", "live": False}, now=self.now),
            "unknown",
        )
        self.assertEqual(
            classify_event_phase({"status": "live", "eventStatus": "scheduled"}, now=self.now),
            "unknown",
        )
        self.assertEqual(
            classify_event_phase({"status": "in-play"}, {"status": "upcoming"}, now=self.now),
            "unknown",
        )
        self.assertEqual(classify_event_phase({"phase": "live"}, now=self.now), "unknown")
        self.assertEqual(
            classify_event_phase({"phase": "live", "_phase_classified": True}, now=self.now),
            "live",
        )

    def test_legacy_launch_does_not_force_live(self):
        with patch.object(bot_live.sys, "argv", ["bot_live.py"]), \
             patch.object(bot_live.subprocess, "Popen") as popen, \
             patch.object(bot_live.subprocess, "run") as run, \
             patch.object(bot_live.secrets, "token_urlsafe", return_value="test-ephemeral-token"):
            bot_live.main()
        command = run.call_args.args[0]
        self.assertIn("--watch", command)
        self.assertNotIn("--live", command)
        self.assertEqual(run.call_args.kwargs["cwd"], str(bot_live.HERE))
        popen.assert_called_once()
        backend_token = popen.call_args.kwargs["env"]["POLYARB_SCANNER_WS_TOKEN"]
        scanner_token = run.call_args.kwargs["env"]["POLYARB_SCANNER_WS_TOKEN"]
        self.assertEqual(backend_token, "test-ephemeral-token")
        self.assertEqual(scanner_token, backend_token)


class PMCatalogRefreshTests(unittest.TestCase):
    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return []

    def test_market_map_prunes_only_after_complete_refresh(self):
        current = {"old": {"v": 1}, "keep": {"v": 1}}
        fresh = {"keep": {"v": 2}, "new": {"v": 1}}
        merged, added, removed = polyarb_v4.reconcile_pm_market_map(current, fresh, True)
        self.assertEqual(set(merged), {"keep", "new"})
        self.assertEqual(merged["keep"]["v"], 2)
        self.assertEqual((added, removed), (1, 1))

        merged, added, removed = polyarb_v4.reconcile_pm_market_map(current, fresh, False)
        self.assertEqual(set(merged), {"old", "keep", "new"})
        self.assertEqual((added, removed), (1, 0))

    def test_fetch_reports_complete_empty_catalog(self):
        with patch.object(polyarb_v4.requests, "get", return_value=self.FakeResponse()) as get:
            markets, complete = polyarb_v4.fetch_live_pm_markets(with_status=True)
        self.assertEqual(markets, {})
        self.assertTrue(complete)
        self.assertEqual(get.call_count, 14)

    def test_fetch_marks_any_failed_query_as_partial(self):
        responses = [self.FakeResponse() for _ in range(13)] + [OSError("temporary")]
        with patch.object(polyarb_v4.requests, "get", side_effect=responses):
            markets, complete = polyarb_v4.fetch_live_pm_markets(with_status=True)
        self.assertEqual(markets, {})
        self.assertFalse(complete)

    def test_limit_sized_response_cannot_authorize_pruning(self):
        class CappedResponse(self.FakeResponse):
            def json(self):
                return [{"markets": []} for _ in range(200)]

        responses = [self.FakeResponse() for _ in range(13)] + [CappedResponse()]
        with patch.object(polyarb_v4.requests, "get", side_effect=responses):
            markets, complete = polyarb_v4.fetch_live_pm_markets(with_status=True)
        self.assertEqual(markets, {})
        self.assertFalse(complete)

    def test_non_list_outcomes_or_tokens_mark_catalog_partial(self):
        class WrongShapeResponse(self.FakeResponse):
            def json(self):
                return [{"markets": [
                    {
                        "slug": "cs2-scalar-shape",
                        "outcomes": json.dumps("ab"),
                        "clobTokenIds": json.dumps("xy"),
                    },
                    {
                        "slug": "cs2-object-shape",
                        "outcomes": json.dumps({"0": "A", "1": "B"}),
                        "clobTokenIds": json.dumps({"0": "x", "1": "y"}),
                    },
                ]}]

        responses = [WrongShapeResponse() for _ in range(14)]
        with patch.object(polyarb_v4.requests, "get", side_effect=responses):
            markets, complete = polyarb_v4.fetch_live_pm_markets(with_status=True)
        self.assertEqual(markets, {})
        self.assertFalse(complete)


class PMBookRecoveryTests(unittest.TestCase):
    class FakeResponse:
        def __init__(self, book):
            self.book = book

        def raise_for_status(self):
            return None

        def json(self):
            return self.book

    class InlineExecutor:
        def submit(self, function, *args):
            return function(*args)

    def setUp(self):
        self.tokens = ["unit-test-pm-book-%s-%d" % (id(self), i) for i in range(4)]
        self._clear_state()

    def tearDown(self):
        self._clear_state()

    def _clear_state(self):
        for token in getattr(self, "tokens", []):
            with polyarb_v4.LOCK:
                polyarb_v4.BOOKS.pop(token, None)
            with polyarb_v4._PM_REFRESH_LOCK:
                polyarb_v4._PM_REFRESH_INFLIGHT.discard(token)
                polyarb_v4._PM_REFRESH_COOLDOWN.pop(token, None)
                polyarb_v4._PM_REFRESH_DIAGNOSTICS.pop(token, None)

    def test_book_diagnostic_distinguishes_missing_empty_stale_and_usable(self):
        now = time.time()
        with polyarb_v4.LOCK:
            polyarb_v4.BOOKS[self.tokens[1]] = {
                "bids": [(0.4, 2.0)], "asks": [], "ts": now,
                "last_change": now,
            }
            polyarb_v4.BOOKS[self.tokens[2]] = {
                "bids": [], "asks": [], "ts": now, "last_change": now,
            }
            polyarb_v4.BOOKS[self.tokens[3]] = {
                "bids": [(0.4, 2.0)], "asks": [(0.5, 3.0)],
                "ts": now - polyarb_v4.BOOK_TTL - 1,
                "last_change": now - polyarb_v4.BOOK_TTL - 1,
            }
        self.assertEqual(
            polyarb_v4.pm_book_diagnostic(self.tokens[0], now)["book_state"],
            "missing_book",
        )
        self.assertEqual(
            polyarb_v4.pm_book_diagnostic(self.tokens[1], now)["book_state"],
            "empty_asks",
        )
        self.assertEqual(
            polyarb_v4.pm_book_diagnostic(self.tokens[2], now)["book_state"],
            "empty_book",
        )
        self.assertEqual(
            polyarb_v4.pm_book_diagnostic(self.tokens[3], now)["book_state"],
            "stale_book",
        )

    def test_missing_book_is_fetched_and_empty_ask_is_reported(self):
        token = self.tokens[0]
        response = self.FakeResponse({
            "bids": [{"price": "0.40", "size": "5"}],
            "asks": [],
        })
        with patch.object(polyarb_v4, "_PM_REFRESH_EXECUTOR", self.InlineExecutor()):
            with patch.object(polyarb_v4.requests, "get", return_value=response) as get:
                self.assertTrue(polyarb_v4._maybe_refresh_stale(token))
        diagnostic = polyarb_v4.pm_book_diagnostic(token)
        self.assertEqual(diagnostic["book_state"], "empty_asks")
        self.assertEqual(diagnostic["rest_refresh"], "empty_asks")
        self.assertEqual(diagnostic["bid_levels"], 1)
        self.assertEqual(diagnostic["ask_levels"], 0)
        get.assert_called_once()

    def test_missing_book_recovery_restores_executable_ask(self):
        token = self.tokens[1]
        response = self.FakeResponse({
            "bids": [],
            "asks": [{"price": "0.37", "size": "12"}],
        })
        with patch.object(polyarb_v4, "_PM_REFRESH_EXECUTOR", self.InlineExecutor()):
            with patch.object(polyarb_v4.requests, "get", return_value=response):
                self.assertTrue(polyarb_v4._maybe_refresh_stale(token))
        self.assertAlmostEqual(polyarb_v4.best_ask(token), 0.37)
        self.assertEqual(
            polyarb_v4.pm_book_diagnostic(token)["rest_refresh"],
            "asks_available",
        )

    def test_rest_refresh_does_not_overwrite_newer_websocket_book(self):
        token = self.tokens[3]
        now = time.time()
        with polyarb_v4.LOCK:
            polyarb_v4.BOOKS[token] = {
                "bids": [(0.59, 20.0)], "asks": [(0.61, 30.0)],
                "ts": now, "last_change": now,
            }
        with patch.object(
            polyarb_v4, "_fresh_pm_book",
            return_value=([(0.20, 100.0)], [(0.30, 100.0)]),
        ):
            polyarb_v4._refresh_pm_book_worker(token, now - 1.0, "stale_book")
        self.assertAlmostEqual(polyarb_v4.best_ask(token), 0.61)
        self.assertEqual(
            polyarb_v4.pm_book_diagnostic(token)["rest_refresh"],
            "websocket_newer",
        )

    def test_failed_rest_refresh_is_rate_limited(self):
        token = self.tokens[2]
        with patch.object(polyarb_v4, "_PM_REFRESH_EXECUTOR", self.InlineExecutor()):
            with patch.object(polyarb_v4.requests, "get", side_effect=OSError("offline")) as get:
                self.assertTrue(polyarb_v4._maybe_refresh_stale(token))
                self.assertFalse(polyarb_v4._maybe_refresh_stale(token))
        get.assert_called_once()
        self.assertEqual(
            polyarb_v4.pm_book_diagnostic(token)["rest_refresh"],
            "fetch_failed",
        )

    def test_fresh_asks_build_candidates_with_refresh_flag(self):
        now = time.time()
        with polyarb_v4.LOCK:
            for token in self.tokens[:2]:
                polyarb_v4.BOOKS[token] = {
                    "bids": [(0.48, 200.0)], "asks": [(0.50, 200.0)],
                    "ts": now, "last_change": now,
                }
        market = {"o": ["Yes", "No"], "t": self.tokens[:2]}
        with patch.object(polyarb_v4, "_maybe_refresh_stale", return_value=False):
            pairs = polyarb_v4.build_arb_candidates(
                market,
                {"Yes": {"o": 2.0}, "No": {"o": 2.0}},
                0.0,
            )
        self.assertEqual(len(pairs), 2)
        self.assertTrue(all(pair[0]["pm_verified"] is False for pair in pairs))

    def test_no_ask_market_row_contains_market_outcome_and_book_state(self):
        details = []
        market = {"slug": "event-winner", "o": ["Yes", "No"], "t": self.tokens[:2]}
        with patch.object(polyarb_v4, "_maybe_refresh_stale", return_value=False):
            pairs = polyarb_v4.build_arb_candidates(
                market, {"Yes": {"o": 2.0}}, 0.0,
                diagnostics={}, pm_diagnostics=details,
                market_slug="event-winner",
            )
        self.assertEqual(pairs, [])
        self.assertEqual(len(details), 1)
        self.assertEqual(details[0]["market_slug"], "event-winner")
        self.assertEqual(details[0]["outcome"], "Yes")
        self.assertEqual(details[0]["token_id"], self.tokens[0])
        self.assertEqual(details[0]["book_state"], "missing_book")


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
