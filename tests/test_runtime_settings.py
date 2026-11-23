import unittest
import os
from unittest.mock import patch

from runtime_settings import (
    DEFAULT_RUNTIME_SETTINGS,
    ONEWIN_ODDS_MAX_AGE_ENV,
    RUNTIME_LIMIT_DEFAULTS,
    normalize_runtime_settings,
    resolve_onewin_odds_max_age,
    resolve_setting,
    runtime_limits_payload,
    settings_limits_payload,
    validate_cli_overrides,
    validate_setting,
)


class SharedRuntimeSettingsTests(unittest.TestCase):
    def test_onewin_ttl_default_valid_and_invalid_environment_values(self):
        with patch.dict(os.environ, {}, clear=True):
            payload = runtime_limits_payload()
        self.assertEqual(payload["freshness"]["onewin_odds_max_age_sec"], 30.0)
        self.assertEqual(payload["freshness"]["onewin_odds_age_source"], "default")
        self.assertEqual(payload["warnings"], [])

        for raw in ("45", "1", "600", "45.5"):
            with self.subTest(raw=raw):
                self.assertEqual(resolve_onewin_odds_max_age(raw), (float(raw), "env", []))

        for raw in ("invalid", "nan", "inf", "0", "601"):
            with self.subTest(raw=raw):
                value, source, warnings = resolve_onewin_odds_max_age(raw)
                self.assertEqual(value, 30.0)
                self.assertEqual(source, "default")
                self.assertEqual(len(warnings), 1)
                self.assertIn(ONEWIN_ODDS_MAX_AGE_ENV, warnings[0])

    def test_runtime_limits_preserve_existing_fixed_operational_values(self):
        with patch.dict(os.environ, {}, clear=True):
            payload = runtime_limits_payload()
        self.assertEqual(payload["freshness"]["polymarket_book_ttl_sec"], 30.0)
        self.assertEqual(payload["freshness"]["polymarket_stale_diagnostic_sec"], 15.0)
        self.assertEqual(payload["freshness"]["dashboard_source_stale_sec"], 10.0)
        self.assertEqual(payload["catalog"]["gamma_events_per_page"], 100)
        self.assertEqual(payload["catalog"]["gamma_max_pages_per_query"], 50)
        self.assertEqual(payload["catalog"]["gamma_retries_per_page"], 3)
        self.assertEqual(payload["catalog"]["gamma_workers"], 7)
        self.assertEqual(payload["safety"]["bankroll_plan_fraction"], 0.40)
        self.assertEqual(RUNTIME_LIMIT_DEFAULTS["bankroll_plan_fraction"], 0.40)

    def test_common_defaults_and_limits_are_one_contract(self):
        settings, warnings, migrated = normalize_runtime_settings({})
        self.assertEqual(settings, DEFAULT_RUNTIME_SETTINGS)
        self.assertEqual(warnings, [])
        self.assertTrue(migrated)
        limits = settings_limits_payload()
        for name, spec in limits.items():
            self.assertEqual(spec["default"], DEFAULT_RUNTIME_SETTINGS[name])
            self.assertEqual(validate_setting(spec["default"], name), spec["default"])

    def test_precedence_is_cli_then_saved_config(self):
        config = {"bank": 250.0}
        self.assertEqual(resolve_setting(config, 500, "bank"), (500.0, "cli"))
        self.assertEqual(resolve_setting(config, None, "bank"), (250.0, "config"))

    def test_legacy_bank_is_converted_to_usdt_without_silent_default(self):
        settings, warnings, migrated = normalize_runtime_settings({
            "bank": 2500, "rate": 85, "currency": "RUB",
        })
        self.assertEqual(settings["bank"], 29.41)
        self.assertEqual(warnings, [])
        self.assertTrue(migrated)

    def test_invalid_values_fall_back_with_explicit_diagnostics(self):
        settings, warnings, _migrated = normalize_runtime_settings({
            "currency": "USDT", "bank": 0, "min_roi_pct": 101,
            "poll": "nan", "watch_sec": 1.5,
        })
        self.assertEqual(settings["bank"], 100.0)
        self.assertEqual(settings["min_roi_pct"], 3.0)
        self.assertEqual(settings["poll"], 0.3)
        self.assertEqual(settings["watch_sec"], 20)
        self.assertEqual(len(warnings), 4)
        self.assertTrue(all("config.json" in warning for warning in warnings))

    def test_cli_overrides_are_range_checked_in_both_forms(self):
        validate_cli_overrides([
            "--bank", "50", "--min-roi=0", "--poll", "0.05",
            "--watch-sec=3600", "--alert-gap", "0",
        ])
        for args in (
            ["--bank", "0"],
            ["--min-roi", "101"],
            ["--poll=0.01"],
            ["--watch-sec", "1.5"],
            ["--alert-gap", "-1"],
            ["--bank"],
        ):
            with self.subTest(args=args), self.assertRaises(ValueError):
                validate_cli_overrides(args)


if __name__ == "__main__":
    unittest.main()
