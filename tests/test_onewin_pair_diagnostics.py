import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import bk_router
import onewin_bridge as ow
import polyarb_v4


class PairDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.stream = ow.OneWinStream()
        self.stream._store_matches([{
            "id": 123, "sportId": 142,
            "homeTeam": {"name": "Team Alpha"},
            "awayTeam": {"name": "Team Bravo"},
        }])
        self.slug = "cs2-team-alpha-team-bravo-2026-11-19"
        self.stream.bind(self.slug, ["Team Alpha", "Team Bravo"])
        self.stream._connected.set()

    def add_quotes(self, quotes, group_id="winner"):
        self.stream.stakes.setdefault("123", {})[group_id] = {
            "name": "Winner", "isBase": True,
            "odds": {str(index): odd for index, odd in enumerate(quotes)},
        }

    def test_one_stale_side_is_reported_without_producing_a_pair(self):
        self.add_quotes([
            {"name": "Team Alpha", "cf": 1.9, "status": 1, "receivedAt": 998.0},
            {"name": "Team Bravo", "cf": 2.1, "status": 1, "receivedAt": 950.0},
        ])
        with patch.object(ow.time, "time", return_value=1000.0), \
             patch.object(ow, "ODDS_MAX_AGE_SECONDS", 30.0):
            pair, details = self.stream.inspect_live_bc(self.slug)
            self.assertEqual(self.stream.live_bc(self.slug), {})
        self.assertEqual(pair, {})
        self.assertEqual(details["reason"], "one_side_not_fresh")
        self.assertTrue(details["subscription_desired"])
        self.assertFalse(details["odds_subscription_sent"])
        self.assertEqual(details["sides"][0]["fresh"], 1)
        self.assertEqual(details["sides"][0]["last_quote_age_sec"], 2.0)
        self.assertEqual(details["sides"][1]["stale"], 1)
        self.assertEqual(details["sides"][1]["last_quote_age_sec"], 50.0)

    def test_fresh_pair_with_draw_is_rejected(self):
        self.add_quotes([
            {"name": "Team Alpha", "cf": 1.9, "status": 1, "receivedAt": 998.0},
            {"name": "Team Bravo", "cf": 2.1, "status": 1, "receivedAt": 999.0},
            {"name": "Draw", "cf": 3.2, "status": 1, "receivedAt": 999.0},
        ])
        with patch.object(ow.time, "time", return_value=1000.0):
            pair, details = self.stream.inspect_live_bc(self.slug)
            self.assertEqual(pair, self.stream.live_bc(self.slug))
        self.assertEqual(details["reason"], "unsupported_outcomes")
        self.assertEqual(details["unmapped_outcomes"], 1)
        self.assertEqual(pair, {})

    def test_reversed_pm_sides_report_correct_team(self):
        self.stream.bind(self.slug, ["Team Bravo", "Team Alpha"])
        self.add_quotes([
            {"name": "Team Alpha", "cf": 1.9, "status": 1, "receivedAt": 950.0},
            {"name": "Team Bravo", "cf": 2.1, "status": 1, "receivedAt": 998.0},
        ])
        with patch.object(ow.time, "time", return_value=1000.0), \
             patch.object(ow, "ODDS_MAX_AGE_SECONDS", 30.0):
            _, details = self.stream.inspect_live_bc(self.slug)
        self.assertEqual(details["sides"][0]["outcome"], "Team Bravo")
        self.assertEqual(details["sides"][0]["fresh"], 1)
        self.assertEqual(details["sides"][1]["outcome"], "Team Alpha")
        self.assertEqual(details["sides"][1]["stale"], 1)

    def test_missing_and_ambiguous_groups_remain_fail_closed(self):
        self.assertEqual(self.stream.diagnose_live_bc(self.slug)["reason"], "missing_market_group")
        quotes = [
            {"name": "Team Alpha", "cf": 1.9, "status": 1, "receivedAt": 998.0},
            {"name": "Team Bravo", "cf": 2.1, "status": 1, "receivedAt": 998.0},
        ]
        self.add_quotes(quotes, "group-a")
        self.add_quotes(quotes, "group-b")
        with patch.object(ow.time, "time", return_value=1000.0):
            pair, details = self.stream.inspect_live_bc(self.slug)
        self.assertEqual(pair, {})
        self.assertEqual(details["reason"], "ambiguous_market_groups")
        self.assertEqual(details["matching_groups"], 2)

    def test_present_but_empty_market_group_has_its_own_stage_and_stays_rejected(self):
        self.stream.stakes.setdefault("123", {})["winner"] = {
            "name": "Winner", "isBase": True, "odds": {},
        }
        pair, details = self.stream.inspect_live_bc(self.slug)
        self.assertEqual(pair, {})
        self.assertEqual(details["reason"], "market_group_empty")
        self.assertEqual(details["stage"], "outcomes")
        self.assertEqual(details["matching_groups"], 1)
        self.assertEqual(details["outcome_count"], 0)

    def test_diagnostic_exposes_event_specific_receipt_ages(self):
        self.add_quotes([])
        self.stream._subscription_sent_at["123"] = 990.0
        self.stream._last_match_info_at["123"] = 992.0
        self.stream._last_match_odds_message_at["123"] = 994.0
        self.stream._last_match_price_update_at["123"] = 995.0
        with patch.object(ow.time, "time", return_value=1000.0):
            _, details = self.stream.inspect_live_bc(self.slug)
        self.assertEqual(details["subscription_age_sec"], 10.0)
        self.assertEqual(details["match_info_age_sec"], 8.0)
        self.assertEqual(details["odds_message_age_sec"], 6.0)
        self.assertEqual(details["price_update_age_sec"], 5.0)

    def test_resubscribe_retries_only_the_target_bound_match_odds(self):
        ws = SimpleNamespace(send=Mock())
        self.stream._ws = ws
        self.stream._connected.set()
        self.assertTrue(self.stream._resubscribe_match_odds(123, now=1000.0))
        self.assertEqual(ws.send.call_count, 2)
        packets = [call.args[0] for call in ws.send.call_args_list]
        self.assertTrue(all(packet.startswith("42") for packet in packets))
        self.assertEqual(self.stream._subscription_sent_at["123"], 1000.0)
        self.assertEqual(self.stream._subscribed_odds[True], {"123"})
        self.assertEqual(self.stream._subscribed_odds[False], {"123"})

    def test_repair_loop_enforces_per_match_retry_cooldown(self):
        class StopAfterTwoCycles:
            def __init__(self):
                self.results = iter((False, False, True))

            def wait(self, _seconds):
                return next(self.results)

        self.stream._stop = StopAfterTwoCycles()
        self.stream._last_match_price_update_at["123"] = 900.0
        with patch.object(ow.time, "time", side_effect=(1000.0, 1020.0)), \
             patch.object(self.stream, "inspect_live_bc",
                          return_value=({}, {"reason": "no_fresh_sides"})), \
             patch.object(self.stream, "_resubscribe_match_odds", return_value=True) as retry:
            self.stream._repair_loop()
        retry.assert_called_once_with(123, now=1000.0)
        self.assertEqual(self.stream._odds_resubscribe_attempts, 1)

    def test_targeted_catalog_refresh_merges_and_respects_cooldowns(self):
        incoming = [{"id": 789, "homeTeam": {"name": "Gamma"},
                     "awayTeam": {"name": "Delta"}}]
        with patch.object(self.stream, "_fetch_sport", return_value=incoming) as fetch:
            self.assertTrue(self.stream._refresh_bound_sport_catalog(142, now=1000.0))
            self.assertFalse(self.stream._refresh_bound_sport_catalog(142, now=1050.0))
        self.assertEqual(fetch.call_count, 1)
        self.assertIn("123", self.stream.matches)
        self.assertIn("789", self.stream.matches)
        self.assertEqual(self.stream.matches["789"]["sportId"], 142)

    def test_closed_and_missing_receipt_are_not_fresh(self):
        self.add_quotes([
            {"name": "Team Alpha", "cf": 1.9, "status": 0, "receivedAt": 998.0},
            {"name": "Team Bravo", "cf": 2.1, "status": 1},
        ])
        with patch.object(ow.time, "time", return_value=1000.0):
            pair, details = self.stream.inspect_live_bc(self.slug)
        self.assertEqual(pair, {})
        self.assertEqual(details["reason"], "no_fresh_sides")
        self.assertEqual(details["sides"][0]["closed"], 1)
        self.assertEqual(details["sides"][1]["missing_timestamp"], 1)

    def test_exact_age_boundaries_match_hot_and_diagnostic_paths(self):
        for timestamp, accepted in [(1000.0, True), (970.0, True),
                                    (969.999, False), (1000.001, False)]:
            with self.subTest(timestamp=timestamp):
                self.stream.stakes.clear()
                self.add_quotes([
                    {"name": "Team Alpha", "cf": 1.9, "status": 1, "receivedAt": timestamp},
                    {"name": "Team Bravo", "cf": 2.1, "status": 1, "receivedAt": 998.0},
                ])
                with patch.object(ow.time, "time", return_value=1000.0), \
                     patch.object(ow, "ODDS_MAX_AGE_SECONDS", 30.0):
                    pair, details = self.stream.inspect_live_bc(self.slug)
                    self.assertEqual(self.stream.live_bc(self.slug), pair)
                self.assertEqual(bool(pair), accepted)
                self.assertEqual(details["reason"], "fresh" if accepted else "one_side_not_fresh")

    def test_hot_path_does_not_wait_for_subscription_send_lock(self):
        self.add_quotes([
            {"name": "Team Alpha", "cf": 1.9, "status": 1, "receivedAt": 998.0},
            {"name": "Team Bravo", "cf": 2.1, "status": 1, "receivedAt": 999.0},
        ])
        send_lock = Mock()
        send_lock.__enter__ = Mock(side_effect=AssertionError("hot path acquired send lock"))
        send_lock.__exit__ = Mock()
        with patch.object(ow.time, "time", return_value=1000.0), \
             patch.object(self.stream, "_send_lock", send_lock):
            self.assertTrue(self.stream.live_bc(self.slug))

    def test_diagnostic_does_not_wait_for_blocked_network_send(self):
        holding, release = threading.Event(), threading.Event()
        def hold_send_lock():
            with self.stream._send_lock:
                holding.set()
                release.wait(3)
        worker = threading.Thread(target=hold_send_lock, daemon=True)
        worker.start()
        self.assertTrue(holding.wait(2))
        try:
            diagnostic = self.stream.diagnose_live_bc(self.slug)
            self.assertIsNone(diagnostic["odds_subscription_sent"])
            self.assertEqual(diagnostic["reason"], "missing_market_group")
        finally:
            release.set()
            worker.join(2)
        self.assertFalse(worker.is_alive())

    def test_feed_updates_replace_published_group_and_odds(self):
        data = {"matchId": 123, "oddsGroups": [{"id": "winner", "name": "Winner",
                "oddsList": [{"id": "1", "name": "Team Alpha", "cf": 1.9, "status": 1}]}]}
        self.stream._on_match_odds(data, snapshot=True)
        old_group = self.stream.stakes["123"]["winner"]
        self.stream._on_match_odds({"matchId": 123, "oddsGroups": [{"id": "winner",
                                    "oddsList": [{"id": "1", "cf": 2.0}]}]})
        new_group = self.stream.stakes["123"]["winner"]
        self.assertIsNot(old_group, new_group)
        self.assertEqual(old_group["odds"]["1"]["cf"], 1.9)
        self.assertEqual(new_group["odds"]["1"]["cf"], 2.0)

    def test_duplicate_fresh_side_is_rejected(self):
        self.add_quotes([
            {"name": "Team Alpha", "cf": 1.9, "status": 1, "receivedAt": 998.0},
            {"name": "Team Alpha", "cf": 1.95, "status": 1, "receivedAt": 998.0},
            {"name": "Team Bravo", "cf": 2.1, "status": 1, "receivedAt": 999.0},
        ])
        with patch.object(ow.time, "time", return_value=1000.0):
            pair, details = self.stream.inspect_live_bc(self.slug)
        self.assertEqual(pair, {})
        self.assertEqual(details["reason"], "unsupported_outcomes")

    def test_duplicate_labels_are_mapped_once_but_still_counted_individually(self):
        self.add_quotes([
            {"name": "Team Alpha", "cf": 1.9, "status": 1, "receivedAt": 998.0},
            {"name": "Team Alpha", "cf": 1.95, "status": 1, "receivedAt": 998.0},
            {"name": "Team Bravo", "cf": 2.1, "status": 1, "receivedAt": 999.0},
        ])
        with patch.object(ow.time, "time", return_value=1000.0), \
             patch.object(ow, "_team_from_outcome", wraps=ow._team_from_outcome) as map_outcome:
            pair, details = self.stream.inspect_live_bc(self.slug)
        self.assertEqual(map_outcome.call_count, 2)
        self.assertEqual(pair, {})
        self.assertEqual(details["reason"], "unsupported_outcomes")
        self.assertEqual(details["sides"][0]["fresh"], 2)

    def test_router_statistics_scope_only_includes_requested_bound_markets(self):
        other = "cs2-team-alpha-team-bravo-2026-11-19-game1"
        self.stream.bind(other, ["Team Alpha", "Team Bravo"])
        adapter = SimpleNamespace(get_global_stream=lambda: self.stream)
        with patch.object(bk_router, "_ow", adapter), \
             patch.object(bk_router, "_load_onewin", return_value=adapter):
            router = bk_router.BKRouter(use_bb=False, use_onewin=True)
            router._onewin_stream = self.stream
            router.bindings = {
                self.slug: ("1win", {"match_id": 123}),
                other: ("1win", {"match_id": 123}),
                "ignored-bb": ("BetBoom", {"match_id": 456}),
            }
            stats = router.stats([self.slug])
            details = router.onewin_pair_diagnostic(self.slug)
        self.assertEqual(stats["bound_1win"], 1)
        self.assertEqual(stats["onewin_fresh_pairs"], 0)
        self.assertEqual(stats["onewin_pair_diagnostics"]["reason_counts"], {"missing_market_group": 1})
        self.assertEqual(stats["onewin_pair_diagnostics"]["stage_counts"], {"market_group": 1})
        self.assertEqual(details["reason"], "missing_market_group")

    def test_diagnostics_do_not_create_a_stream(self):
        adapter = SimpleNamespace(get_global_stream=Mock(side_effect=AssertionError("started a stream")))
        with patch.object(bk_router, "_ow", adapter), \
             patch.object(bk_router, "_load_onewin", return_value=adapter):
            router = bk_router.BKRouter(use_bb=False, use_onewin=True)
            router.bindings = {self.slug: ("1win", {"match_id": 123})}
            self.assertIsNone(router.onewin_pair_diagnostic(self.slug))
            self.assertEqual(router.stats()["onewin_fresh_pairs"], 0)
        adapter.get_global_stream.assert_not_called()

    def test_drop_preserves_current_pm_and_onewin_details(self):
        diagnostic = {"reason": "one_side_not_fresh", "sides": [{"outcome": "Team Alpha", "stale": 1}]}
        drop = polyarb_v4._make_signal_drop_diagnostic(
            {"slug": self.slug}, "no_fresh_bookmaker_pair", 1000.0,
            current_pm_diagnostic={"book_state": "missing_book", "rest_refresh": "queued"},
            current_onewin_diagnostic=diagnostic,
        )
        self.assertEqual(drop["current_onewin_pair"], diagnostic)
        self.assertEqual(drop["current_pm_book"]["state"], "missing_book")
        self.assertEqual(drop["current_pm_book"]["rest_refresh"], "queued")


if __name__ == "__main__":
    unittest.main()
