import json
import sys
import threading
import unittest
import time
from unittest.mock import patch

import price_history
import stats
import arb_invariants
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
        self.assertEqual(command[command.index("--min-roi") + 1], "3.0")
        self.assertNotIn("--bank", command)
        self.assertNotIn("--live", command)
        self.assertEqual(run.call_args.kwargs["cwd"], str(bot_live.HERE))
        popen.assert_called_once()
        backend_token = popen.call_args.kwargs["env"]["POLYARB_SCANNER_WS_TOKEN"]
        scanner_token = run.call_args.kwargs["env"]["POLYARB_SCANNER_WS_TOKEN"]
        self.assertEqual(backend_token, "test-ephemeral-token")
        self.assertEqual(scanner_token, backend_token)


class PMCatalogRefreshTests(unittest.TestCase):
    class FakeResponse:
        def __init__(self, payload=None, status_code=200):
            self.payload = payload if payload is not None else {
                "events": [], "next_cursor": "LTE="
            }
            self.status_code = status_code

        def raise_for_status(self):
            if self.status_code >= 400:
                raise polyarb_v4.requests.HTTPError(
                    "HTTP %s" % self.status_code, response=self
                )
            return None

        def json(self):
            return self.payload

    @staticmethod
    def _catalog_query(tag, mode, *, events=None, error=None, exhausted=True,
                       pages=1, page_cap_hit=False):
        return {
            "tag": tag, "mode": mode, "pages": pages,
            "raw_event_rows": len(events or []), "invalid_event_rows": 0,
            "events": list(events or []), "cursor_exhausted": exhausted,
            "page_cap_hit": page_cap_hit, "effective_page_limit": 500,
            "last_http_status": None, "error": error,
        }

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

    def test_catalog_requests_are_parallel_and_have_bounded_timeouts(self):
        lock = threading.Lock()
        active = 0
        max_active = 0
        bad_timeouts = []

        def fake_get(*args, **kwargs):
            nonlocal active, max_active
            if kwargs.get("timeout") != polyarb_v4.PM_CATALOG_TIMEOUT:
                bad_timeouts.append(kwargs.get("timeout"))
            with lock:
                active += 1
                max_active = max(max_active, active)
            time.sleep(0.03)
            with lock:
                active -= 1
            return self.FakeResponse()

        with patch.object(polyarb_v4.requests, "get", side_effect=fake_get) as get:
            markets, complete = polyarb_v4.fetch_live_pm_markets(with_status=True)

        diagnostic = polyarb_v4.get_pm_catalog_diagnostics()
        self.assertEqual(markets, {})
        self.assertTrue(complete)
        self.assertEqual(get.call_count, 14)
        self.assertGreater(max_active, 1)
        self.assertEqual(bad_timeouts, [])
        self.assertTrue(all(call.args[0].endswith("/events/keyset") for call in get.call_args_list))
        self.assertEqual(diagnostic["successful_queries"], 14)
        self.assertEqual(diagnostic["failed_queries"], 0)
        self.assertEqual(diagnostic["total_pages"], 14)
        self.assertEqual(diagnostic["cursor_exhausted_queries"], 14)

    def test_fetch_marks_any_failed_query_as_partial(self):
        def fake_tag(tag, _query_limit):
            results = []
            for mode in ("live", "general"):
                if tag == "cs2" and mode == "general":
                    results.append(self._catalog_query(
                        tag, mode, error="OSError", exhausted=False, pages=0
                    ))
                else:
                    results.append(self._catalog_query(tag, mode))
            return tag, results

        with patch.object(polyarb_v4, "_fetch_pm_catalog_tag", side_effect=fake_tag):
            markets, complete = polyarb_v4.fetch_live_pm_markets(with_status=True)
        self.assertEqual(markets, {})
        self.assertFalse(complete)
        diagnostic = polyarb_v4.get_pm_catalog_diagnostics()
        self.assertEqual(diagnostic["failed_queries"], 1)
        self.assertEqual(diagnostic["error_counts"], {"OSError": 1})

    def test_full_page_with_terminal_cursor_is_complete(self):
        response = self.FakeResponse({
            "events": [{"markets": []} for _ in range(500)],
            "next_cursor": "LTE=",
        })
        with patch.object(polyarb_v4.requests, "get", return_value=response):
            markets, complete = polyarb_v4.fetch_live_pm_markets(with_status=True)
        self.assertEqual(markets, {})
        self.assertTrue(complete)

    def test_keyset_pages_are_merged_and_duplicate_market_slugs_are_deduplicated(self):
        def market_event(slug):
            return {"markets": [{
                "slug": slug,
                "question": slug,
                "outcomes": json.dumps(["Yes", "No"]),
                "clobTokenIds": json.dumps([slug + "-y", slug + "-n"]),
            }]}

        def fake_get(_url, *, params, **_kwargs):
            if params.get("tag_slug") == "cs2" and "live" not in params:
                if params.get("after_cursor") == "page-2":
                    return self.FakeResponse({
                        "events": [market_event("cs2-beta"), market_event("cs2-alpha")],
                        "next_cursor": "LTE=",
                    })
                return self.FakeResponse({
                    "events": [market_event("cs2-alpha")],
                    "next_cursor": "page-2",
                })
            return self.FakeResponse()

        with patch.object(polyarb_v4.requests, "get", side_effect=fake_get) as get:
            markets, complete = polyarb_v4.fetch_live_pm_markets(with_status=True)
        self.assertTrue(complete)
        self.assertEqual(set(markets), {"cs2-alpha", "cs2-beta"})
        self.assertEqual(get.call_count, 15)
        diagnostic = polyarb_v4.get_pm_catalog_diagnostics()
        self.assertEqual(diagnostic["query_count"], 14)
        self.assertEqual(diagnostic["total_pages"], 15)
        self.assertEqual(diagnostic["successful_queries"], 14)

    def test_single_query_sends_after_cursor_and_stops_on_terminal_cursor(self):
        pages = [
            self.FakeResponse({"events": [{"id": "e1"}], "next_cursor": "cursor-1"}),
            self.FakeResponse({"events": [{"id": "e2"}], "next_cursor": "LTE="}),
        ]
        with patch.object(polyarb_v4.requests, "get", side_effect=pages) as get:
            result = polyarb_v4._fetch_pm_catalog_query("cs2", "general")
        self.assertTrue(result["cursor_exhausted"])
        self.assertIsNone(result["error"])
        self.assertEqual(result["pages"], 2)
        self.assertEqual([event["id"] for event in result["events"]], ["e1", "e2"])
        self.assertEqual(get.call_args_list[0].kwargs["params"]["limit"], 500)
        self.assertEqual(get.call_args_list[1].kwargs["params"]["after_cursor"], "cursor-1")
        self.assertNotIn("offset", get.call_args_list[1].kwargs["params"])

    def test_repeated_cursor_fails_closed(self):
        pages = [
            self.FakeResponse({"events": [{"id": "e1"}], "next_cursor": "cursor-1"}),
            self.FakeResponse({"events": [{"id": "e2"}], "next_cursor": "cursor-1"}),
        ]
        with patch.object(polyarb_v4.requests, "get", side_effect=pages):
            result = polyarb_v4._fetch_pm_catalog_query("cs2", "general")
        self.assertFalse(result["cursor_exhausted"])
        self.assertEqual(result["error"], "RepeatedCursor")
        self.assertEqual(result["pages"], 2)

    def test_page_cap_fails_closed_without_claiming_cursor_exhaustion(self):
        pages = [
            self.FakeResponse({"events": [], "next_cursor": "cursor-%d" % i})
            for i in range(1, 3)
        ]
        with patch.object(polyarb_v4, "PM_CATALOG_MAX_PAGES", 2):
            with patch.object(polyarb_v4.requests, "get", side_effect=pages):
                result = polyarb_v4._fetch_pm_catalog_query("cs2", "general")
        self.assertFalse(result["cursor_exhausted"])
        self.assertTrue(result["page_cap_hit"])
        self.assertEqual(result["error"], "PageCapReached")
        self.assertEqual(result["pages"], 2)

    def test_page_error_keeps_rows_but_reports_incomplete_stream(self):
        responses = [
            self.FakeResponse({"events": [{"id": "e1"}], "next_cursor": "cursor-1"}),
            OSError("temporary"), OSError("temporary"), OSError("temporary"),
        ]
        with patch.object(polyarb_v4.time, "sleep"):
            with patch.object(polyarb_v4.requests, "get", side_effect=responses):
                result = polyarb_v4._fetch_pm_catalog_query("cs2", "general")
        self.assertFalse(result["cursor_exhausted"])
        self.assertEqual(result["error"], "OSError")
        self.assertEqual(result["pages"], 1)
        self.assertEqual(result["events"], [{"id": "e1"}])

    def test_http_429_is_retried_with_bounded_timeout(self):
        responses = [self.FakeResponse(status_code=429), self.FakeResponse()]
        with patch.object(polyarb_v4.time, "sleep"):
            with patch.object(polyarb_v4.requests, "get", side_effect=responses) as get:
                result = polyarb_v4._fetch_pm_catalog_query("cs2", "general")
        self.assertTrue(result["cursor_exhausted"])
        self.assertIsNone(result["error"])
        self.assertEqual(get.call_count, 2)
        self.assertTrue(all(
            call.kwargs["timeout"] == polyarb_v4.PM_CATALOG_TIMEOUT
            for call in get.call_args_list
        ))

    def test_non_list_outcomes_or_tokens_mark_catalog_partial(self):
        class WrongShapeResponse(self.FakeResponse):
            def json(self):
                return {"events": [{"markets": [
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
                ]}], "next_cursor": "LTE="}

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

    def test_book_diagnostic_reports_local_and_source_event_ages(self):
        now = time.time()
        with polyarb_v4.LOCK:
            polyarb_v4.BOOKS[self.tokens[0]] = {
                "bids": [(0.40, 2.0)], "asks": [(0.50, 3.0)],
                "ts": now - 3.0, "last_change": now - 3.0,
                "source_ts": now - 7.0,
            }
        diagnostic = polyarb_v4.pm_book_diagnostic(self.tokens[0], now)
        self.assertEqual(diagnostic["book_age_sec"], 3.0)
        self.assertEqual(diagnostic["source_event_age_sec"], 7.0)
        self.assertEqual(diagnostic["source_event_ts"], now - 7.0)

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

    def test_rest_book_reader_extracts_documented_snapshot_timestamp(self):
        response = self.FakeResponse({
            "timestamp": "1763700000",
            "bids": [{"price": "0.40", "size": "5"}],
            "asks": [{"price": "0.50", "size": "7"}],
        })
        with patch.object(polyarb_v4.requests, "get", return_value=response):
            bids, asks, source_ts = polyarb_v4._fresh_pm_book(self.tokens[2])
        self.assertEqual(bids, [(0.40, 5.0)])
        self.assertEqual(asks, [(0.50, 7.0)])
        self.assertEqual(source_ts, 1763700000.0)

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

    def test_rest_snapshot_timestamp_becomes_the_source_watermark(self):
        token = self.tokens[0]
        now = time.time()
        with polyarb_v4.LOCK:
            polyarb_v4.BOOKS[token] = {
                "bids": [(0.40, 5.0)], "asks": [(0.50, 7.0)],
                "ts": now - 20.0, "last_change": now - 20.0,
                "source_ts": 1763700000.0,
            }
        with patch.object(
            polyarb_v4, "_fresh_pm_book",
            return_value=([(0.41, 6.0)], [(0.51, 8.0)], 1763700100.0),
        ):
            polyarb_v4._refresh_pm_book_worker(token, now - 10.0, "stale_book")
        with polyarb_v4.LOCK:
            self.assertEqual(polyarb_v4.BOOKS[token]["source_ts"], 1763700100.0)
            self.assertEqual(polyarb_v4.BOOKS[token]["asks"], [(0.51, 8.0)])

    def test_newer_rest_snapshot_replaces_late_arriving_older_ws_book(self):
        token = self.tokens[2]
        now = time.time()
        with polyarb_v4.LOCK:
            polyarb_v4.BOOKS[token] = {
                "bids": [(0.40, 5.0)], "asks": [(0.50, 7.0)],
                "ts": now, "last_change": now,
                "source_ts": 1763699999.0,
            }
        with patch.object(
            polyarb_v4, "_fresh_pm_book",
            return_value=([(0.42, 6.0)], [(0.49, 8.0)], 1763700000.0),
        ):
            polyarb_v4._refresh_pm_book_worker(token, now - 1.0, "stale_book")
        with polyarb_v4.LOCK:
            self.assertEqual(polyarb_v4.BOOKS[token]["asks"], [(0.49, 8.0)])
            self.assertEqual(polyarb_v4.BOOKS[token]["source_ts"], 1763700000.0)

    def test_older_rest_snapshot_does_not_replace_newer_source_book(self):
        token = self.tokens[1]
        now = time.time()
        with polyarb_v4.LOCK:
            polyarb_v4.BOOKS[token] = {
                "bids": [(0.40, 5.0)], "asks": [(0.50, 7.0)],
                "ts": now - 20.0, "last_change": now - 20.0,
                "source_ts": 1763700000.0,
            }
        with patch.object(
            polyarb_v4, "_fresh_pm_book",
            return_value=([(0.30, 6.0)], [(0.35, 8.0)], 1763699999.0),
        ):
            polyarb_v4._refresh_pm_book_worker(token, now - 10.0, "stale_book")
        with polyarb_v4.LOCK:
            self.assertEqual(polyarb_v4.BOOKS[token]["asks"], [(0.50, 7.0)])
        self.assertEqual(
            polyarb_v4.pm_book_diagnostic(token)["rest_refresh"],
            "rest_snapshot_older",
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
        self.assertTrue(all(polyarb_v4._is_pm_bookmaker_pair(pair) for pair in pairs))
        self.assertTrue(all(
            sum(leg.get("source") == "Polymarket" for leg in pair) == 1
            and len(pair) == 2
            for pair in pairs
        ))
        for pair in pairs:
            pm_leg = next(leg for leg in pair if leg["source"] == "Polymarket")
            bookmaker_leg = next(leg for leg in pair if leg["source"] != "Polymarket")
            self.assertEqual({pm_leg["outcome"], bookmaker_leg["outcome"]}, {"Yes", "No"})
            self.assertNotEqual(pm_leg["outcome"], bookmaker_leg["outcome"])

    def test_candidate_builder_rejects_polymarket_as_the_bookmaker_source(self):
        market = {"o": ["Yes", "No"], "t": self.tokens[:2]}
        now = time.time()
        with polyarb_v4.LOCK:
            for token in self.tokens[:2]:
                polyarb_v4.BOOKS[token] = {
                    "bids": [(0.48, 100.0)], "asks": [(0.50, 100.0)],
                    "ts": now, "last_change": now,
                }
        with patch.object(polyarb_v4, "_maybe_refresh_stale", return_value=False):
            pairs = polyarb_v4.build_arb_candidates(
                market,
                {"Yes": {"o": 2.0}, "No": {"o": 2.0}},
                0.0,
                bk_source="Polymarket",
            )
        self.assertEqual(pairs, [])

    def test_pair_validator_rejects_same_source_or_wrong_leg_count(self):
        pm = {
            "source": "Polymarket", "outcome": "Alpha",
            "token": "pm-token", "o": 2.0,
        }
        one_bookmaker = {"source": "1win", "outcome": "Bravo", "o": 2.0}
        second_bookmaker = {"source": "BetBoom", "outcome": "Charlie", "o": 2.1}
        self.assertTrue(polyarb_v4._is_pm_bookmaker_pair([pm, one_bookmaker]))
        self.assertFalse(polyarb_v4._is_pm_bookmaker_pair([pm, dict(pm)]))
        self.assertFalse(polyarb_v4._is_pm_bookmaker_pair([
            pm, {"source": "1win", "outcome": "Alpha", "o": 2.0}
        ]))
        self.assertFalse(
            polyarb_v4._is_pm_bookmaker_pair([one_bookmaker, second_bookmaker])
        )
        self.assertFalse(polyarb_v4._is_pm_bookmaker_pair(
            [pm, one_bookmaker], expected_bookmaker_source="BetBoom"
        ))
        self.assertFalse(polyarb_v4._is_pm_bookmaker_pair([
            pm, {"source": "UnknownBook", "outcome": "Bravo", "o": 2.0}
        ]))
        self.assertFalse(polyarb_v4._is_pm_bookmaker_pair(
            [pm, {"source": "1win", "outcome": "Bravo", "o": float("nan")}],
            require_pm_token=True,
            require_prices=True,
        ))
        self.assertFalse(
            polyarb_v4._is_pm_bookmaker_pair([pm, one_bookmaker, second_bookmaker])
        )

    def test_alternate_monitor_requires_explicit_pm_and_single_bk_kinds(self):
        pm = {
            "source": "POLYMARKET", "sourceKind": "pm",
            "outcome": "Alpha", "o": 2.1,
        }
        book = {
            "source": "LocalBook", "sourceKind": "bc",
            "outcome": "Bravo", "o": 2.2,
        }
        other_book = {
            "source": "SecondBook", "sourceKind": "bc",
            "outcome": "Alpha", "o": 2.3,
        }
        self.assertTrue(arb_invariants.is_pm_bookmaker_pair(
            [pm, book], source_kind_key="sourceKind",
            allow_unregistered_bookmaker=True, require_prices=True,
        ))
        self.assertFalse(arb_invariants.is_pm_bookmaker_pair(
            [book, other_book], source_kind_key="sourceKind",
            allow_unregistered_bookmaker=True, require_prices=True,
        ))
        self.assertFalse(arb_invariants.is_pm_bookmaker_pair(
            [pm, other_book], source_kind_key="sourceKind",
            allow_unregistered_bookmaker=True, require_prices=True,
        ))

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


class PMStreamEventTests(unittest.TestCase):
    def setUp(self):
        self.token = "unit-test-pm-stream-%s" % id(self)
        with polyarb_v4.LOCK:
            polyarb_v4.BOOKS.pop(self.token, None)
        with polyarb_v4._PM_REFRESH_LOCK:
            polyarb_v4._PM_REFRESH_INFLIGHT.discard(self.token)
            polyarb_v4._PM_REFRESH_COOLDOWN.pop(self.token, None)

    def tearDown(self):
        with polyarb_v4.LOCK:
            polyarb_v4.BOOKS.pop(self.token, None)
        with polyarb_v4._PM_REFRESH_LOCK:
            polyarb_v4._PM_REFRESH_INFLIGHT.discard(self.token)
            polyarb_v4._PM_REFRESH_COOLDOWN.pop(self.token, None)
            polyarb_v4._PM_REFRESH_DIAGNOSTICS.pop(self.token, None)

    def test_clob_timestamp_normalizes_milliseconds_and_seconds(self):
        self.assertEqual(
            polyarb_v4._parse_clob_event_timestamp("1763700000000"),
            1763700000.0,
        )
        self.assertEqual(
            polyarb_v4._parse_clob_event_timestamp(1763700000),
            1763700000.0,
        )
        self.assertIsNone(polyarb_v4._parse_clob_event_timestamp("not-a-time"))

    def test_feed_sends_a_market_ping_within_ten_seconds(self):
        if not polyarb_v4.HAS_WS:
            self.skipTest("websocket-client is not installed")
        clock = {"now": 0.0, "reads": 0}
        sent = []

        class FakeWebSocket:
            def send(self, message):
                sent.append((message, clock["now"]))

            def settimeout(self, _timeout):
                return None

            def recv(self):
                clock["now"] += 1.0
                clock["reads"] += 1
                if clock["reads"] >= 11:
                    polyarb_v4.STOP.set()
                return ""

            def close(self):
                return None

        polyarb_v4.STOP.clear()
        try:
            with patch.object(polyarb_v4.websocket, "create_connection", return_value=FakeWebSocket()):
                with patch.object(polyarb_v4.time, "monotonic", side_effect=lambda: clock["now"]):
                    polyarb_v4.Feed([self.token]).run()
        finally:
            polyarb_v4.STOP.clear()
        ping_times = [at for message, at in sent if message == "PING"]
        self.assertTrue(ping_times, "feed did not send a heartbeat during the first 10 seconds")
        self.assertLessEqual(ping_times[0], 10.0)

    def test_valid_snapshot_and_delta_keep_event_time_and_dispatch_after_commit(self):
        updates = []
        feed = polyarb_v4.Feed([])
        with patch.object(polyarb_v4, "_PM_UPDATE_CALLBACK", updates.append):
            feed._a({
                "event_type": "book", "asset_id": self.token,
                "timestamp": "1763699999000",
                "bids": [{"price": "0.40", "size": "5"}],
                "asks": [{"price": "0.50", "size": "7"}],
            })
            updates.clear()
            feed._a({
                "event_type": "price_change", "timestamp": "1763700000000",
                "price_changes": [{
                    "asset_id": self.token, "side": "SELL",
                    "price": "0.50", "size": "3",
                    "best_bid": "0.40", "best_ask": "0.50",
                }],
            })
        with polyarb_v4.LOCK:
            book = dict(polyarb_v4.BOOKS[self.token])
        self.assertEqual(book["asks"], [(0.50, 3.0)])
        self.assertEqual(book["source_ts"], 1763700000.0)
        self.assertEqual(updates, [self.token])

    def test_older_delta_does_not_change_a_newer_book(self):
        current = time.time()
        newer_timestamp = 1763700000.0
        initial = {
            "bids": [(0.40, 5.0)], "asks": [(0.50, 7.0)],
            "ts": current, "last_change": current,
            "source_ts": newer_timestamp,
        }
        with polyarb_v4.LOCK:
            polyarb_v4.BOOKS[self.token] = dict(initial)
        polyarb_v4.Feed([])._a({
            "event_type": "price_change",
            "timestamp": "1763699999000",
            "price_changes": [{
                "asset_id": self.token, "side": "SELL",
                "price": "0.50", "size": "1",
            }],
        })
        with polyarb_v4.LOCK:
            self.assertEqual(polyarb_v4.BOOKS[self.token], initial)

    def test_older_snapshot_does_not_replace_a_newer_book(self):
        current = time.time()
        initial = {
            "bids": [(0.40, 5.0)], "asks": [(0.50, 7.0)],
            "ts": current, "last_change": current,
            "source_ts": 1763700000.0,
        }
        with polyarb_v4.LOCK:
            polyarb_v4.BOOKS[self.token] = dict(initial)
        polyarb_v4.Feed([])._a({
            "event_type": "book", "asset_id": self.token,
            "timestamp": "1763699999000",
            "bids": [{"price": "0.45", "size": "2"}],
            "asks": [{"price": "0.55", "size": "3"}],
        })
        with polyarb_v4.LOCK:
            self.assertEqual(polyarb_v4.BOOKS[self.token], initial)

    def test_delta_without_snapshot_requests_rest_recovery(self):
        with patch.object(polyarb_v4, "_schedule_pm_book_refresh") as schedule:
            polyarb_v4.Feed([])._a({
                "event_type": "price_change",
                "timestamp": "1763700000000",
                "price_changes": [{
                    "asset_id": self.token, "side": "BUY",
                    "price": "0.40", "size": "5",
                }],
            })
        schedule.assert_called_once_with(self.token, "missing_snapshot")
        with polyarb_v4.LOCK:
            self.assertNotIn(self.token, polyarb_v4.BOOKS)

    def test_best_bid_mismatch_invalidates_book_and_requests_recovery(self):
        current = time.time()
        with polyarb_v4.LOCK:
            polyarb_v4.BOOKS[self.token] = {
                "bids": [(0.40, 5.0)], "asks": [(0.50, 7.0)],
                "ts": current, "last_change": current,
                "source_ts": 1763699999.0,
            }
        with patch.object(polyarb_v4, "_schedule_pm_book_refresh") as schedule:
            polyarb_v4.Feed([])._a({
                "event_type": "price_change",
                "timestamp": "1763700000000",
                "price_changes": [{
                    "asset_id": self.token, "side": "BUY",
                    "price": "0.60", "size": "2",
                    "best_bid": "0.55", "best_ask": "0.50",
                }],
            })
        schedule.assert_called_once_with(self.token, "top_of_book_mismatch")
        with polyarb_v4.LOCK:
            self.assertNotIn(self.token, polyarb_v4.BOOKS)

    def test_best_ask_mismatch_invalidates_book_and_requests_recovery(self):
        current = time.time()
        with polyarb_v4.LOCK:
            polyarb_v4.BOOKS[self.token] = {
                "bids": [(0.40, 5.0)], "asks": [(0.50, 7.0)],
                "ts": current, "last_change": current,
                "source_ts": 1763699999.0,
            }
        with patch.object(polyarb_v4, "_schedule_pm_book_refresh") as schedule:
            polyarb_v4.Feed([])._a({
                "event_type": "price_change",
                "timestamp": "1763700000000",
                "price_changes": [{
                    "asset_id": self.token, "side": "SELL",
                    "price": "0.50", "size": "2",
                    "best_bid": "0.40", "best_ask": "0.51",
                }],
            })
        schedule.assert_called_once_with(self.token, "top_of_book_mismatch")
        with polyarb_v4.LOCK:
            self.assertNotIn(self.token, polyarb_v4.BOOKS)


class CalculationTests(unittest.TestCase):
    def test_risk_capped_plan_bank_is_forty_percent(self):
        plan_bank = polyarb_v4._risk_capped_plan_bank(100.0)
        self.assertEqual(plan_bank, 40.0)
        result = polyarb_v4.compute([
            {"o": 2.1, "c": 0.0, "s": 0.0},
            {"o": 2.1, "c": 0.0, "s": 0.0},
        ], plan_bank)
        self.assertAlmostEqual(sum(result["stakes"]), 40.0, places=8)
        self.assertEqual(polyarb_v4._risk_capped_plan_bank(-1.0), 0.0)

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

    def test_refine_uses_usdt_stakes_without_scaling_odds_by_fx_rate(self):
        token = "unit-test-token-rate"
        book = {"bids": [(0.49, 10000.0)], "asks": [(0.50, 10000.0)],
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
                initial = polyarb_v4.compute(bets, 100.0)
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
