import unittest

from runtime_settings import (
    DEFAULT_RUNTIME_SETTINGS,
    normalize_runtime_settings,
    resolve_setting,
    settings_limits_payload,
    validate_cli_overrides,
    validate_setting,
)


class SharedRuntimeSettingsTests(unittest.TestCase):
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
