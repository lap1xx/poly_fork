"""Shared validation contract for read-only scanner runtime settings."""
from __future__ import annotations

import argparse
import math


SETTING_SPECS = {
    "bank": {
        "label": "Общий банк",
        "minimum": 1.0,
        "maximum": 1_000_000_000.0,
        "default": 100.0,
        "kind": "float",
        "unit": "USDT",
    },
    "min_roi_pct": {
        "label": "Минимальный ROI",
        "minimum": 0.0,
        "maximum": 100.0,
        "default": 3.0,
        "kind": "float",
        "unit": "%",
    },
    "poll": {
        "label": "Интервал обработки",
        "minimum": 0.05,
        "maximum": 30.0,
        "default": 0.3,
        "kind": "float",
        "unit": "с",
    },
    "watch_sec": {
        "label": "Интервал обновления каталога",
        "minimum": 1,
        "maximum": 3600,
        "default": 20,
        "kind": "int",
        "unit": "с",
    },
    "alert_gap": {
        "label": "Интервал повторного сигнала",
        "minimum": 0,
        "maximum": 86400,
        "default": 120,
        "kind": "int",
        "unit": "с",
    },
    "safe_fork_threshold": {
        "label": "Порог цены Polymarket",
        "minimum": 0.01,
        "maximum": 0.99,
        "default": 0.45,
        "kind": "float",
        "unit": "доля",
    },
    "roi_revision": {
        "label": "Версия настройки ROI",
        "minimum": 0,
        "maximum": 2_147_483_647,
        "default": 0,
        "kind": "int",
        "unit": "",
    },
}

DEFAULT_RUNTIME_SETTINGS = {
    name: spec["default"] for name, spec in SETTING_SPECS.items()
}

CLI_OPTIONS = {
    "--bank": "bank",
    "--min-roi": "min_roi_pct",
    "--poll": "poll",
    "--watch-sec": "watch_sec",
    "--alert-gap": "alert_gap",
}


def validate_setting(value, name):
    """Return a finite numeric value in range or raise ValueError."""
    spec = SETTING_SPECS[name]
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        raise ValueError(f"{spec['label']}: требуется числовое значение") from None
    if not math.isfinite(number):
        raise ValueError(f"{spec['label']}: значение должно быть конечным числом")
    if number < spec["minimum"] or number > spec["maximum"]:
        low, high = spec["minimum"], spec["maximum"]
        raise ValueError(
            f"{spec['label']}: допустимый диапазон {low}–{high} {spec['unit']}".strip()
        )
    if spec["kind"] == "int":
        if not number.is_integer():
            raise ValueError(f"{spec['label']}: требуется целое число")
        return int(number)
    return number


def argparse_type(name):
    """Build an argparse type using the shared setting bounds."""
    def parse(value):
        try:
            return validate_setting(value, name)
        except ValueError as exc:
            raise argparse.ArgumentTypeError(str(exc)) from None
    return parse


def normalize_runtime_settings(raw):
    """Return normalized values, user-visible warnings, and legacy-migration flag."""
    raw = raw if isinstance(raw, dict) else {}
    settings = dict(DEFAULT_RUNTIME_SETTINGS)
    warnings = []

    currency = str(raw.get("currency", "")).strip().upper()
    legacy_migrated = currency != "USDT"
    has_bank = "bank" in raw
    if not has_bank:
        settings["bank"] = SETTING_SPECS["bank"]["default"]
    elif legacy_migrated:
        try:
            legacy_bank = float(raw.get("bank"))
            legacy_rate = float(raw.get("rate"))
            if (not math.isfinite(legacy_bank) or legacy_bank <= 0
                    or not math.isfinite(legacy_rate) or legacy_rate <= 0):
                raise ValueError
            settings["bank"] = validate_setting(
                round(legacy_bank / legacy_rate, 2), "bank"
            )
        except (TypeError, ValueError, OverflowError):
            settings["bank"] = SETTING_SPECS["bank"]["default"]
            warnings.append(
                "Старое значение банка нельзя безопасно перевести в USDT; "
                "используется значение по умолчанию 100 USDT."
            )
    else:
        try:
            settings["bank"] = validate_setting(raw.get("bank"), "bank")
        except ValueError:
            settings["bank"] = SETTING_SPECS["bank"]["default"]
            warnings.append(
                "Некорректный банк в config.json; используется значение "
                "по умолчанию 100 USDT."
            )

    for name in ("min_roi_pct", "poll", "watch_sec", "alert_gap",
                 "safe_fork_threshold", "roi_revision"):
        if name not in raw:
            continue
        try:
            settings[name] = validate_setting(raw[name], name)
        except ValueError:
            settings[name] = SETTING_SPECS[name]["default"]
            warnings.append(
                f"Некорректное значение «{SETTING_SPECS[name]['label']}» "
                f"в config.json; используется безопасное значение "
                f"{SETTING_SPECS[name]['default']}."
            )
    return settings, warnings, legacy_migrated


def resolve_setting(config, cli_value, name):
    """Resolve a field using CLI-over-config precedence and return its source."""
    if cli_value is not None:
        return validate_setting(cli_value, name), "cli"
    value = config.get(name, SETTING_SPECS[name]["default"])
    return validate_setting(value, name), "config"


def validate_cli_overrides(tokens):
    """Validate numeric CLI overrides early; returns no value and has no side effects."""
    tokens = list(tokens)
    for index, token in enumerate(tokens):
        for option, name in CLI_OPTIONS.items():
            if token == option:
                if index + 1 >= len(tokens) or tokens[index + 1].startswith("--"):
                    raise ValueError(f"{option}: требуется значение")
                validate_setting(tokens[index + 1], name)
            elif token.startswith(option + "="):
                validate_setting(token.split("=", 1)[1], name)


def settings_limits_payload():
    """JSON-safe limits shared by scanner API and dashboard form."""
    return {
        name: {
            "minimum": spec["minimum"],
            "maximum": spec["maximum"],
            "default": spec["default"],
            "unit": spec["unit"],
        }
        for name, spec in SETTING_SPECS.items()
        if name != "roi_revision"
    }
