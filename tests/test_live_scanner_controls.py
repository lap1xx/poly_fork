import asyncio
import json
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock, patch

import bk_router
import polyarb_v4
import web_app


class RuntimeConfigTests(unittest.TestCase):
    def test_source_normalizer_accepts_only_supported_feeds(self):
        self.assertEqual(
            web_app._normalize_enabled_bk_sources(["onewin", "bb", "unknown"]),
            ["1win", "BetBoom"],
        )

    def test_dashboard_saves_roi_revision_and_feed_selection(self):
        with TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.json"
            with patch.object(web_app, "CONFIG_FILE", config_path), \
                 patch.object(web_app, "_current_scanner_status", return_value={}):
                result = asyncio.run(web_app.api_config_save({
                    "min_roi_pct": 4.5,
                    "enabled_bk_sources": ["1win", "BetBoom"],
                }))
            self.assertTrue(result["ok"])
            self.assertEqual(result["config"]["min_roi_pct"], 4.5)
            self.assertEqual(result["config"]["enabled_bk_sources"], ["1win", "BetBoom"])
            saved = json.loads(config_path.read_text(encoding="utf-8"))
            self.assertEqual(saved["roi_revision"], 1)

    def test_settings_update_migrates_legacy_bank_and_saves_shared_runtime_fields(self):
        with TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.json"
            config_path.write_text(json.dumps({
                "bank": 2500, "rate": 85, "poll": 0.3,
            }), encoding="utf-8")
            with patch.object(web_app, "CONFIG_FILE", config_path), \
                 patch.object(web_app, "_current_scanner_status", return_value={}):
                result = asyncio.run(web_app.api_config_save({
                    "poll": 0.5, "watch_sec": 45, "alert_gap": 0,
                }))
            self.assertTrue(result["ok"])
            self.assertEqual(result["config"]["bank"], 29.41)
            self.assertEqual(result["config"]["poll"], 0.5)
            self.assertEqual(result["config"]["watch_sec"], 45)
            self.assertEqual(result["config"]["alert_gap"], 0)
            saved = json.loads(config_path.read_text(encoding="utf-8"))
            self.assertEqual(saved["currency"], "USDT")
            self.assertEqual(saved["bank"], 29.41)

    def test_settings_api_rejects_values_outside_shared_ranges(self):
        for payload in ({"bank": 0.5}, {"poll": 0.01}, {"watch_sec": 1.5},
                        {"alert_gap": 86401}, {"safe_fork_threshold": 1.0}):
            with self.subTest(payload=payload), TemporaryDirectory() as tmp:
                config_path = Path(tmp) / "config.json"
                with patch.object(web_app, "CONFIG_FILE", config_path), \
                     patch.object(web_app, "_current_scanner_status", return_value={}):
                    result = asyncio.run(web_app.api_config_save(payload))
                self.assertEqual(result.status_code, 422)
                self.assertFalse(config_path.exists())

    def test_stopped_scanner_has_no_active_feeds_even_when_selection_is_saved(self):
        with TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.json"
            config_path.write_text(json.dumps({
                "enabled_bk_sources": ["1win"],
                "min_roi_pct": 4.0,
            }), encoding="utf-8")
            with patch.object(web_app, "CONFIG_FILE", config_path), \
                 patch.object(web_app, "_current_scanner_status", return_value={}):
                config = web_app._runtime_config_payload()
        self.assertFalse(config["scanner_running"])
        self.assertEqual(config["enabled_bk_sources"], ["1win"])
        self.assertEqual(config["active_bk_sources"], [])

    def test_config_payload_exposes_effective_runtime_settings_and_sources(self):
        status = {
            "state": "running", "ts": time.time(),
            "enabled_bk_sources": ["1win"], "available_bk_sources": ["1win"],
            "min_roi_pct": 4.5,
            "runtime_settings": {
                "bank_usdt": 200.0, "min_roi_pct": 4.5, "poll_sec": 0.5,
                "watch_sec": 30, "alert_gap_sec": 0,
                "sources": {"bank": "cli", "min_roi_pct": "config"},
            },
            "config_warnings": ["invalid saved value"],
        }
        with TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.json"
            config_path.write_text(json.dumps({"currency": "USDT", "bank": 100}),
                                   encoding="utf-8")
            with patch.object(web_app, "CONFIG_FILE", config_path), \
                 patch.object(web_app, "_current_scanner_status", return_value=status):
                config = web_app._runtime_config_payload()
        self.assertEqual(config["active_runtime_settings"], status["runtime_settings"])
        self.assertEqual(config["config_warnings"], status["config_warnings"])
        self.assertEqual(config["config_limits"]["poll"]["minimum"], 0.05)

    def test_scanner_and_backend_config_expose_identical_runtime_limits(self):
        with patch.dict("os.environ", {"ONEWIN_MAX_ODDS_AGE_SECONDS": "45"}):
            scanner_limits = polyarb_v4.runtime_limits_payload()
            backend_limits = web_app.runtime_limits_payload()
            status = {
                "state": "running", "ts": time.time(),
                "runtime_limits": scanner_limits,
            }
            with TemporaryDirectory() as tmp:
                config_path = Path(tmp) / "config.json"
                config_path.write_text(json.dumps({"currency": "USDT", "bank": 100}),
                                       encoding="utf-8")
                with patch.object(web_app, "CONFIG_FILE", config_path), \
                     patch.object(web_app, "_current_scanner_status", return_value=status):
                    config = web_app._runtime_config_payload()
        self.assertEqual(scanner_limits, backend_limits)
        self.assertEqual(config["runtime_limits"], scanner_limits)
        self.assertEqual(config["runtime_limits"]["freshness"]["onewin_odds_max_age_sec"], 45.0)
        self.assertEqual(config["runtime_limits"]["freshness"]["onewin_odds_age_source"], "env")

    def test_invalid_onewin_ttl_is_reported_by_backend_config_api(self):
        with patch.dict("os.environ", {"ONEWIN_MAX_ODDS_AGE_SECONDS": "not-a-number"}):
            with TemporaryDirectory() as tmp:
                config_path = Path(tmp) / "config.json"
                config_path.write_text(json.dumps({"currency": "USDT", "bank": 100}),
                                       encoding="utf-8")
                with patch.object(web_app, "CONFIG_FILE", config_path), \
                     patch.object(web_app, "_current_scanner_status", return_value={}):
                    config = web_app._runtime_config_payload()
        self.assertEqual(config["runtime_limits"]["freshness"]["onewin_odds_max_age_sec"], 30.0)
        self.assertEqual(config["runtime_limits"]["freshness"]["onewin_odds_age_source"], "default")
        self.assertTrue(any("ONEWIN_MAX_ODDS_AGE_SECONDS" in warning
                            for warning in config["config_warnings"]))

    def test_dashboard_rejects_empty_or_unknown_feed_selection(self):
        for selected in ([], ["unknown"]):
            with self.subTest(selected=selected), TemporaryDirectory() as tmp:
                config_path = Path(tmp) / "config.json"
                with patch.object(web_app, "CONFIG_FILE", config_path), \
                     patch.object(web_app, "_current_scanner_status", return_value={}):
                    result = asyncio.run(web_app.api_config_save({
                        "enabled_bk_sources": selected,
                    }))
                self.assertEqual(result.status_code, 422)
                self.assertFalse(config_path.exists())

    def test_dashboard_rejects_feed_not_available_in_current_run(self):
        status = {
            "state": "running",
            "ts": time.time(),
            "available_bk_sources": ["1win"],
            "enabled_bk_sources": ["1win"],
            "min_roi_pct": 3.0,
        }
        with TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.json"
            with patch.object(web_app, "CONFIG_FILE", config_path), \
                 patch.object(web_app, "_current_scanner_status", return_value=status):
                result = asyncio.run(web_app.api_config_save({
                    "enabled_bk_sources": ["BetBoom"],
                }))
            self.assertEqual(result.status_code, 409)
            self.assertFalse(config_path.exists())

    def test_scanner_config_normalizes_corrupt_roi_revision_and_feed_values(self):
        with TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.json"
            config_path.write_text(json.dumps({
                "min_roi_pct": "nan",
                "roi_revision": "bad",
                "enabled_bk_sources": ["not-a-feed"],
            }), encoding="utf-8")
            with patch.object(polyarb_v4, "CFG_FILE", str(config_path)):
                cfg = polyarb_v4.load_cfg()
        self.assertEqual(cfg["min_roi_pct"], 3.0)
        self.assertEqual(cfg["roi_revision"], 0)
        self.assertEqual(cfg["enabled_bk_sources"], ["1win"])

    def test_scanner_reads_nested_settings_with_flat_config_precedence(self):
        with TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.json"
            config_path.write_text(json.dumps({
                "currency": "USDT",
                "scan": {"bank": 200, "poll": 0.5, "min_roi_pct": 4.0},
                "bot": {"watch_sec": 30, "alert_gap": 60},
                "poll": 0.75,
            }), encoding="utf-8")
            with patch.object(polyarb_v4, "CFG_FILE", str(config_path)):
                cfg = polyarb_v4.load_cfg()
        self.assertEqual(cfg["bank"], 200)
        self.assertEqual(cfg["poll"], 0.75)
        self.assertEqual(cfg["min_roi_pct"], 4.0)
        self.assertEqual(cfg["watch_sec"], 30)
        self.assertEqual(cfg["alert_gap"], 60)

    def test_malformed_scanner_config_falls_back_to_safe_defaults(self):
        with TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.json"
            config_path.write_text("{broken", encoding="utf-8")
            with patch.object(polyarb_v4, "CFG_FILE", str(config_path)):
                cfg = polyarb_v4.load_cfg()
        self.assertEqual(cfg["min_roi_pct"], 3.0)
        self.assertEqual(cfg["enabled_bk_sources"], ["1win"])

    def test_no_bb_is_hard_deny_even_if_config_and_available_allow_it(self):
        self.assertEqual(
            polyarb_v4.select_runtime_sources(
                ["1win", "BetBoom"], ["1win", "BetBoom"], no_bb=True
            ),
            ["1win"],
        )
        self.assertEqual(
            polyarb_v4.select_runtime_sources(
                ["BetBoom"], ["BetBoom"], no_bb=True
            ),
            [],
        )


class RuntimeRouterSelectionTests(unittest.TestCase):
    def test_hot_switch_clears_old_bindings_and_uses_only_selected_feed(self):
        one_win = SimpleNamespace(
            bind_slug=Mock(return_value={"provider": "1win"}),
            get_live_bc=Mock(return_value={}),
        )
        betboom = SimpleNamespace(
            bind_slug=Mock(return_value={"provider": "BetBoom"}),
            get_live_bc=Mock(return_value={}),
        )
        with patch.object(bk_router, "BB_OK", True), \
             patch.object(bk_router, "_bb", betboom), \
             patch.object(bk_router, "_ow", one_win), \
             patch.object(bk_router, "_load_onewin", return_value=one_win):
            router = bk_router.BKRouter(use_bb=True, use_onewin=True)
            slug = "cs2-team-alpha-team-bravo-winner"
            market = {"o": ["Team Alpha", "Team Bravo"]}
            _, initial_source = router.bind(slug, market)
            self.assertEqual(initial_source, "1win")
            self.assertIn(slug, router.fallback_bindings)

            self.assertTrue(router.configure_sources(["BetBoom"]))
            self.assertEqual(router.active_sources(), ["BetBoom"])
            self.assertNotIn(slug, router.bindings)
            self.assertNotIn(slug, router.fallback_bindings)

            _, selected_source = router.bind(slug, market)
            self.assertEqual(selected_source, "BetBoom")
            self.assertEqual(router.get_src(slug), "BetBoom")
            self.assertEqual(one_win.bind_slug.call_count, 1)
            self.assertEqual(betboom.bind_slug.call_count, 2)

    def test_router_refuses_selection_of_disallowed_or_empty_source(self):
        one_win = SimpleNamespace(bind_slug=Mock(return_value={"provider": "1win"}))
        with patch.object(bk_router, "BB_OK", True), \
             patch.object(bk_router, "_ow", one_win), \
             patch.object(bk_router, "_load_onewin", return_value=one_win):
            router = bk_router.BKRouter(use_bb=False, use_onewin=True)
            self.assertFalse(router.configure_sources(["BetBoom"]))
            self.assertFalse(router.configure_sources([]))
            self.assertEqual(router.active_sources(), ["1win"])


if __name__ == "__main__":
    unittest.main()
