import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import bk_router


class BKRouterPriorityTests(unittest.TestCase):
    def _router(self, one_win_bind, bb_bind, one_win_live=None, bb_live=None):
        one_win = SimpleNamespace(
            bind_slug=Mock(return_value=one_win_bind),
            get_live_bc=Mock(return_value=one_win_live or {}),
        )
        betboom = SimpleNamespace(
            bind_slug=Mock(return_value=bb_bind),
            get_live_bc=Mock(return_value=bb_live or {}),
        )
        stack = (
            patch.object(bk_router, "BB_OK", True),
            patch.object(bk_router, "_bb", betboom),
            patch.object(bk_router, "_ow", one_win),
            patch.object(bk_router, "_load_onewin", return_value=one_win),
        )
        for p in stack:
            p.start()
            self.addCleanup(p.stop)
        return bk_router.BKRouter(use_bb=True, use_onewin=True), one_win, betboom

    def test_onewin_is_primary_even_for_live_map_market(self):
        router, one_win, betboom = self._router(
            {"provider": "1win"}, {"provider": "BetBoom"}
        )
        binding, source = router.bind(
            "cs2-team-alpha-team-bravo-map3-winner-live",
            {"o": ["Team Alpha", "Team Bravo"]},
        )
        self.assertEqual(source, "1win")
        self.assertEqual(binding["provider"], "1win")
        one_win.bind_slug.assert_called_once()
        betboom.bind_slug.assert_called_once()  # retained as live fallback

    def test_betboom_is_used_when_onewin_cannot_bind_market(self):
        router, one_win, betboom = self._router(None, {"provider": "BetBoom"})
        binding, source = router.bind(
            "cs2-team-alpha-team-bravo-map-total",
            {"o": ["Team Alpha", "Team Bravo"]},
        )
        self.assertEqual(source, "BetBoom")
        self.assertEqual(binding["provider"], "BetBoom")
        one_win.bind_slug.assert_called_once()
        betboom.bind_slug.assert_called_once()

    def test_betboom_live_quotes_are_used_when_primary_bind_has_no_quotes(self):
        bb_live = {"Team Bravo": {"src": "BetBoom", "o": 2.2, "c": 0.0}}
        router, one_win, betboom = self._router(
            {"provider": "1win"}, {"provider": "BetBoom"}, {}, bb_live
        )
        router.bind("cs2-team-alpha-team-bravo-winner", {"o": ["Team Alpha", "Team Bravo"]})
        self.assertEqual(router.live_bc("cs2-team-alpha-team-bravo-winner"), bb_live)
        self.assertEqual(router.get_src("cs2-team-alpha-team-bravo-winner"), "BetBoom")
        self.assertEqual(router.get_binding("cs2-team-alpha-team-bravo-winner"), {"provider": "BetBoom"})
        one_win.get_live_bc.assert_called_once()
        betboom.get_live_bc.assert_called_once()

    def test_unready_onewin_does_not_capture_market_from_ready_betboom(self):
        router, one_win, betboom = self._router(None, {"provider": "BetBoom"})
        self.assertFalse(router.wait_onewin(timeout=0, min_matches=1))
        binding, source = router.bind(
            "cs2-team-alpha-team-bravo-winner",
            {"o": ["Team Alpha", "Team Bravo"]},
        )
        self.assertEqual(source, "BetBoom")
        self.assertEqual(binding["provider"], "BetBoom")
        one_win.bind_slug.assert_not_called()
        betboom.bind_slug.assert_called_once()

    def test_existing_betboom_binding_promotes_back_to_recovered_onewin(self):
        one_live = {"Team Alpha": {"src": "1win", "o": 1.8, "c": 0.0}}
        router, one_win, betboom = self._router(
            {"provider": "1win"}, {"provider": "BetBoom"}, one_live,
            {"Team Alpha": {"src": "BetBoom", "o": 1.7, "c": 0.0}},
        )
        ready = Mock(return_value=False)
        one_win.get_global_stream = Mock(return_value=SimpleNamespace(is_ready=ready))
        router._onewin_ready = False
        slug = "cs2-team-alpha-team-bravo-winner"
        router.bind(slug, {"o": ["Team Alpha", "Team Bravo"]})
        self.assertEqual(router.get_src(slug), "BetBoom")

        ready.return_value = True
        self.assertEqual(router.live_bc(slug), one_live)
        self.assertEqual(router.get_src(slug), "1win")
        self.assertEqual(router.get_binding(slug), {"provider": "1win"})
        self.assertEqual(router.fallback_bindings[slug], {"provider": "BetBoom"})

    def test_feed_health_reports_onewin_quote_transport_metrics(self):
        expected = {"connected": True, "fresh_open_quotes": 21}
        router, one_win, _ = self._router(None, None)
        stream = SimpleNamespace(snapshot_health=Mock(return_value=expected))
        one_win.get_global_stream = Mock(return_value=stream)
        self.assertEqual(router.feed_health(), {"1win": expected})
        one_win.get_global_stream.assert_called_once_with()

    def test_stats_counts_fresh_pairs_only_for_current_onewin_bindings(self):
        router, one_win, _ = self._router(None, None)
        live_slug = "cs2-alpha-bravo-winner"
        stale_slug = "cs2-charlie-delta-winner"
        bb_slug = "cs2-echo-foxtrot-winner"
        stream = SimpleNamespace(
            matches={"501": {}},
            lock=threading.RLock(),
            live_bc=Mock(side_effect=lambda slug: {"pair": True} if slug == live_slug else {}),
        )
        one_win.get_global_stream = Mock(return_value=stream)
        router.bindings = {
            live_slug: ("1win", {"match_id": 501}),
            stale_slug: ("1win", {"match_id": 502}),
            bb_slug: ("BetBoom", {"match_id": 503}),
        }

        stats = router.stats([live_slug, stale_slug, bb_slug])

        self.assertEqual(stats["bindings"], 3)
        self.assertEqual(stats["bound_1win"], 2)
        self.assertEqual(stats["bound_betboom"], 1)
        self.assertEqual(stats["onewin_fresh_pairs"], 1)
        self.assertEqual(stream.live_bc.call_count, 2)
        stream.live_bc.assert_any_call(live_slug)
        stream.live_bc.assert_any_call(stale_slug)

    def test_invalid_market_shape_is_rejected_before_provider_calls(self):
        router, one_win, betboom = self._router({"provider": "1win"}, {"provider": "BetBoom"})
        self.assertEqual(router.bind("cs2-something", {"o": ["Only one outcome"]}), (None, None))
        one_win.bind_slug.assert_not_called()
        betboom.bind_slug.assert_not_called()


if __name__ == "__main__":
    unittest.main()
