#!/usr/bin/env python3
"""Capture and summarize read-only scanner status for quality-gate baselines.

This utility only reads an existing scanner_status.json file. It never starts
feeds, calls provider APIs, or modifies scanner/backend state.
"""
from __future__ import annotations

import argparse
import json
import math
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_VERSION = 1
MATCHING_FIELDS = (
    "pm_markets",
    "matched_pm_markets",
    "unmatched_pm_markets",
    "bound_1win",
    "onewin_catalog_matches",
    "fresh_1win_pairs",
)
HEALTH_FIELDS = (
    "connected",
    "catalog_ready",
    "catalog_matches",
    "bound_pm_markets",
    "subscribed_matches",
    "fresh_open_quotes",
    "stale_open_quotes",
    "odds_snapshots_received",
    "odds_deltas_received",
    "valid_price_updates_received",
    "last_odds_message_age_sec",
    "last_price_update_age_sec",
    "max_quote_age_sec",
    "open_quote_age_sec",
    "missing_receipt_timestamp_quotes",
    "last_error",
)


def _utc_iso(epoch=None):
    stamp = time.time() if epoch is None else float(epoch)
    return datetime.fromtimestamp(stamp, tz=timezone.utc).isoformat()


def _as_dict(value):
    return value if isinstance(value, dict) else {}


def _finite_number(value):
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _git_commit(project_dir):
    try:
        result = subprocess.run(
            ["git", "-C", str(project_dir), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
            timeout=3,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip() or None


def build_sample(status, sampled_at_epoch=None):
    """Keep only aggregate diagnostics; omit opportunities and account data."""
    if not isinstance(status, dict):
        raise ValueError("scanner status must be a JSON object")
    sampled_at_epoch = time.time() if sampled_at_epoch is None else float(sampled_at_epoch)
    matching = _as_dict(status.get("matching_stats"))
    all_health = _as_dict(status.get("bookmaker_feed_health"))
    onewin_health = _as_dict(all_health.get("1win"))
    health = {key: onewin_health.get(key) for key in HEALTH_FIELDS
              if key in onewin_health}
    return {
        "kind": "sample",
        "sampled_at_epoch": sampled_at_epoch,
        "sampled_at_utc": _utc_iso(sampled_at_epoch),
        "scanner": {
            "state": status.get("state"),
            "ts": status.get("ts"),
            "cycle": status.get("cycle"),
            "update_mode": status.get("update_mode"),
        },
        "scanner_commit": status.get("scanner_commit"),
        "matching_stats": {key: matching.get(key) for key in MATCHING_FIELDS
                            if key in matching},
        "onewin_feed_health": health,
        "pm_catalog": {
            "complete": status.get("pm_catalog_complete"),
            "retained": status.get("pm_catalog_retained"),
            "fetch": _as_dict(status.get("pm_catalog_fetch")),
        },
        "pm_book_health": _as_dict(status.get("pm_book_health")),
        "rolling_event_to_scan_status_latency": _as_dict(
            status.get("rolling_event_to_scan_status_latency")
        ),
        "runtime_limits": _as_dict(status.get("runtime_limits")),
        "enabled_bk_sources": status.get("enabled_bk_sources", []),
    }


def _read_status(path):
    with Path(path).open("r", encoding="utf-8") as handle:
        status = json.load(handle)
    if not isinstance(status, dict):
        raise ValueError("scanner status must be a JSON object")
    if _finite_number(status.get("ts")) is None:
        raise ValueError("scanner status is missing a numeric ts")
    return status


def capture(status_path, output_path, duration_sec=900.0, interval_sec=1.0,
            project_dir=None):
    """Poll a local status file for a fixed duration and write compact JSONL."""
    duration = float(duration_sec)
    interval = float(interval_sec)
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError("duration_sec must be a positive finite number")
    if not math.isfinite(interval) or interval <= 0:
        raise ValueError("interval_sec must be a positive finite number")

    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    started_epoch = time.time()
    started_mono = time.monotonic()
    deadline = started_mono + duration
    metadata = {
        "kind": "capture_metadata",
        "schema_version": SCHEMA_VERSION,
        "started_at_epoch": started_epoch,
        "started_at_utc": _utc_iso(started_epoch),
        "requested_duration_sec": duration,
        "poll_interval_sec": interval,
        "project_commit": _git_commit(project_dir or Path.cwd()),
        "status_path": str(Path(status_path).resolve()),
        "collection_method": "local_read_only_file_poll",
    }

    sample_count = 0
    read_errors = 0
    last_status_key = None
    with output.open("x", encoding="utf-8") as handle:
        handle.write(json.dumps(metadata, ensure_ascii=False, sort_keys=True) + "\n")
        handle.flush()
        while True:
            observed = time.monotonic()
            if observed >= deadline:
                break
            try:
                status = _read_status(status_path)
                status_key = (status.get("ts"), status.get("cycle"))
                if status_key != last_status_key:
                    sample = build_sample(status)
                    handle.write(json.dumps(sample, ensure_ascii=False, sort_keys=True) + "\n")
                    handle.flush()
                    sample_count += 1
                    last_status_key = status_key
            except (OSError, ValueError, json.JSONDecodeError):
                read_errors += 1

            remaining = deadline - time.monotonic()
            if remaining > 0:
                time.sleep(min(interval, remaining))

        ended_epoch = time.time()
        footer = {
            "kind": "capture_end",
            "ended_at_epoch": ended_epoch,
            "ended_at_utc": _utc_iso(ended_epoch),
            "elapsed_sec": round(time.monotonic() - started_mono, 3),
            "sample_count": sample_count,
            "status_read_errors": read_errors,
        }
        handle.write(json.dumps(footer, ensure_ascii=False, sort_keys=True) + "\n")
        handle.flush()
    return {**metadata, **footer}


def _distribution(values):
    clean = sorted(number for value in values
                   if (number := _finite_number(value)) is not None)
    if not clean:
        return {"count": 0, "min": None, "p50": None, "p95": None, "max": None}

    def nearest_rank(fraction):
        index = max(0, math.ceil(fraction * len(clean)) - 1)
        return round(clean[index], 3)

    return {
        "count": len(clean),
        "min": round(clean[0], 3),
        "p50": nearest_rank(0.50),
        "p95": nearest_rank(0.95),
        "max": round(clean[-1], 3),
    }


def summarize_records(records):
    metadata = next((row for row in records if row.get("kind") == "capture_metadata"), {})
    footer = next((row for row in reversed(records) if row.get("kind") == "capture_end"), {})
    samples = [row for row in records if row.get("kind") == "sample"]
    if not samples:
        raise ValueError("capture contains no scanner status samples")

    elapsed = max(0.0, float(samples[-1].get("sampled_at_epoch", 0))
                  - float(samples[0].get("sampled_at_epoch", 0)))
    measures = {
        "pm_markets": [],
        "matched_pm_markets": [],
        "bound_1win": [],
        "fresh_1win_pairs": [],
        "pm_match_coverage_pct": [],
        "onewin_bound_coverage_pct": [],
        "fresh_pairs_of_bound_1win_pct": [],
        "fresh_open_quotes": [],
        "stale_open_quotes": [],
        "last_odds_message_age_sec": [],
        "last_price_update_age_sec": [],
        "onewin_quote_age_p50_sec": [],
        "onewin_quote_age_p95_sec": [],
        "onewin_quote_age_max_sec": [],
        "missing_receipt_timestamp_quotes": [],
        "pm_fresh_books": [],
        "pm_stale_books": [],
        "pm_missing_books": [],
        "pm_book_age_p50_sec": [],
        "pm_book_age_p95_sec": [],
        "pm_book_age_max_sec": [],
        "pm_source_event_age_p50_sec": [],
        "pm_source_event_age_p95_sec": [],
        "pm_source_event_age_max_sec": [],
    }
    connected = []
    catalog_complete = []
    feed_error_samples = 0
    non_running_samples = 0
    latency_p95 = {}

    for row in samples:
        matching = _as_dict(row.get("matching_stats"))
        health = _as_dict(row.get("onewin_feed_health"))
        latency = _as_dict(row.get("rolling_event_to_scan_status_latency"))
        for field in ("pm_markets", "matched_pm_markets", "bound_1win", "fresh_1win_pairs"):
            if field in matching:
                measures[field].append(matching[field])
        pm_count = _finite_number(matching.get("pm_markets"))
        matched = _finite_number(matching.get("matched_pm_markets"))
        bound = _finite_number(matching.get("bound_1win"))
        fresh = _finite_number(matching.get("fresh_1win_pairs"))
        if pm_count and pm_count > 0:
            if matched is not None:
                measures["pm_match_coverage_pct"].append(100.0 * matched / pm_count)
            if bound is not None:
                measures["onewin_bound_coverage_pct"].append(100.0 * bound / pm_count)
        if bound and bound > 0 and fresh is not None:
            measures["fresh_pairs_of_bound_1win_pct"].append(100.0 * fresh / bound)

        for field in (
            "fresh_open_quotes", "stale_open_quotes",
            "last_odds_message_age_sec", "last_price_update_age_sec",
            "missing_receipt_timestamp_quotes",
        ):
            if field in health:
                measures[field].append(health[field])
        onewin_age = _as_dict(health.get("open_quote_age_sec"))
        for field, target in (
            ("p50_sec", "onewin_quote_age_p50_sec"),
            ("p95_sec", "onewin_quote_age_p95_sec"),
            ("max_sec", "onewin_quote_age_max_sec"),
        ):
            if field in onewin_age:
                measures[target].append(onewin_age[field])
        if isinstance(health.get("connected"), bool):
            connected.append(health["connected"])
        catalog = _as_dict(row.get("pm_catalog"))
        if isinstance(catalog.get("complete"), bool):
            catalog_complete.append(catalog["complete"])
        pm_health = _as_dict(row.get("pm_book_health"))
        for field, target in (
            ("fresh_books", "pm_fresh_books"),
            ("stale_books", "pm_stale_books"),
            ("missing_books", "pm_missing_books"),
        ):
            if field in pm_health:
                measures[target].append(pm_health[field])
        for age_key, prefix in (
            ("book_age_sec", "pm_book_age"),
            ("source_event_age_sec", "pm_source_event_age"),
        ):
            age_summary = _as_dict(pm_health.get(age_key))
            for field, suffix in (("p50_sec", "p50"), ("p95_sec", "p95"),
                                  ("max_sec", "max")):
                target = prefix + "_" + suffix + "_sec"
                if field in age_summary:
                    measures[target].append(age_summary[field])
        if health.get("last_error"):
            feed_error_samples += 1
        if _as_dict(row.get("scanner")).get("state") != "running":
            non_running_samples += 1

        for source, stats in _as_dict(latency.get("by_source")).items():
            p95 = _as_dict(stats).get("p95_ms")
            if _finite_number(p95) is not None:
                latency_p95.setdefault(source, []).append(p95)

    latest = samples[-1]
    latest_latency = _as_dict(latest.get("rolling_event_to_scan_status_latency"))
    return {
        "schema_version": SCHEMA_VERSION,
        "scanner_commit": samples[-1].get("scanner_commit"),
        "collector_project_commit": metadata.get("project_commit"),
        "capture_started_at_utc": metadata.get("started_at_utc"),
        "capture_ended_at_utc": footer.get("ended_at_utc"),
        "requested_duration_sec": metadata.get("requested_duration_sec"),
        "observed_sample_span_sec": round(elapsed, 3),
        "sample_count": len(samples),
        "status_read_errors": footer.get("status_read_errors"),
        "non_running_samples": non_running_samples,
        "feed_error_samples": feed_error_samples,
        "connected_share_pct": round(100.0 * sum(connected) / len(connected), 2)
        if connected else None,
        "catalog_complete_share_pct": round(100.0 * sum(catalog_complete) / len(catalog_complete), 2)
        if catalog_complete else None,
        "metrics": {name: _distribution(values) for name, values in measures.items()},
        "rolling_latency_window": {
            "window_sec": latest_latency.get("window_sec"),
            "latest": latest_latency,
            "reported_p95_ms_over_capture": {
                source: _distribution(values) for source, values in sorted(latency_p95.items())
            },
        },
        "latest_runtime_limits": _as_dict(latest.get("runtime_limits")),
        "latest_pm_catalog_fetch": _as_dict(_as_dict(latest.get("pm_catalog")).get("fetch")),
    }


def load_jsonl(path):
    records = []
    with Path(path).open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid JSONL at line {line_number}: {exc}") from exc
            if isinstance(record, dict):
                records.append(record)
    return records


def format_markdown(summary):
    metrics = summary["metrics"]
    def dist(name, unit=""):
        row = metrics[name]
        if not row["count"]:
            return "нет данных"
        return "min {0}{4}; p50 {1}{4}; p95 {2}{4}; max {3}{4} (n={5})".format(
            row["min"], row["p50"], row["p95"], row["max"], unit, row["count"]
        )

    lines = [
        "# Quality-gate baseline",
        "",
        f"- Commit сканера: `{summary.get('scanner_commit') or 'не указан в status'}`",
        f"- Commit коллектора: `{summary.get('collector_project_commit') or 'не указан'}`",
        f"- Выборка: {summary['sample_count']} snapshots за {summary['observed_sample_span_sec']} сек.",
        f"- Ошибки чтения status-файла: {summary.get('status_read_errors')}",
        f"- Доля connected: {summary.get('connected_share_pct')}%",
        f"- Доля полных каталогов PM: {summary.get('catalog_complete_share_pct')}%",
        f"- Сэмплы с ошибкой 1win: {summary.get('feed_error_samples')}; не-running scanner: {summary.get('non_running_samples')}",
        "",
        "## Покрытие и свежесть",
        "",
        f"- PM markets: {dist('pm_markets')}",
        f"- PM matched markets: {dist('matched_pm_markets')}",
        f"- Bound 1win: {dist('bound_1win')}",
        f"- Свежие пары 1win: {dist('fresh_1win_pairs')}",
        f"- Доля сопоставленных PM: {dist('pm_match_coverage_pct', '%')}",
        f"- Доля привязок 1win от PM: {dist('onewin_bound_coverage_pct', '%')}",
        f"- Свежие пары от bound 1win: {dist('fresh_pairs_of_bound_1win_pct', '%')}",
        f"- Открытые свежие котировки 1win: {dist('fresh_open_quotes')}",
        f"- Открытые устаревшие котировки 1win: {dist('stale_open_quotes')}",
        f"- Возраст открытых 1win котировок: p50 {dist('onewin_quote_age_p50_sec', ' сек.')}; p95 {dist('onewin_quote_age_p95_sec', ' сек.')}; max {dist('onewin_quote_age_max_sec', ' сек.')}",
        f"- Возраст последнего price update: {dist('last_price_update_age_sec', ' сек.')}",
        f"- PM fresh/stale/missing books: {dist('pm_fresh_books')} / {dist('pm_stale_books')} / {dist('pm_missing_books')}",
        f"- Возраст локальных PM books: p50 {dist('pm_book_age_p50_sec', ' сек.')}; p95 {dist('pm_book_age_p95_sec', ' сек.')}; max {dist('pm_book_age_max_sec', ' сек.')}",
        f"- Возраст PM source events: p50 {dist('pm_source_event_age_p50_sec', ' сек.')}; p95 {dist('pm_source_event_age_p95_sec', ' сек.')}; max {dist('pm_source_event_age_max_sec', ' сек.')}",
        "",
        "## Latency (receipt → сборка scanner status, не end-to-end)",
        "",
    ]
    latency = summary["rolling_latency_window"]
    lines.append(f"- Окно каждого scanner rolling snapshot: {latency.get('window_sec')} сек.")
    for source, stats in latency.get("reported_p95_ms_over_capture", {}).items():
        lines.append(f"- {source}: распределение сообщаемого p95: {stats}")
    if not latency.get("reported_p95_ms_over_capture"):
        lines.append("- Нет latency samples.")
    return "\n".join(lines) + "\n"


def _capture_command(args):
    result = capture(
        args.status, args.output, args.duration_seconds,
        args.interval_seconds, args.project_dir,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["sample_count"] else 2


def _report_command(args):
    summary = summarize_records(load_jsonl(args.input))
    text = json.dumps(summary, ensure_ascii=False, indent=2) + "\n" \
        if args.format == "json" else format_markdown(summary)
    if args.output:
        Path(args.output).write_text(text, encoding="utf-8")
    else:
        sys.stdout.write(text)
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    cap = commands.add_parser("capture", help="poll existing local scanner_status.json")
    cap.add_argument("--status", default="scanner_status.json")
    cap.add_argument("--output", required=True, help="new JSONL file; existing files are not overwritten")
    cap.add_argument("--duration-seconds", type=float, default=900.0)
    cap.add_argument("--interval-seconds", type=float, default=1.0)
    cap.add_argument("--project-dir", default=str(Path.cwd()))
    cap.set_defaults(func=_capture_command)

    report = commands.add_parser("report", help="summarize a captured JSONL baseline")
    report.add_argument("--input", required=True)
    report.add_argument("--format", choices=("markdown", "json"), default="markdown")
    report.add_argument("--output")
    report.set_defaults(func=_report_command)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    raise SystemExit(main())
