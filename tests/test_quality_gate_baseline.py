import json
import tempfile
import unittest
from pathlib import Path

from tools.quality_gate_baseline import (
    build_sample,
    capture,
    format_markdown,
    load_jsonl,
    summarize_records,
)


def _sample(epoch, *, connected=True, error=None, catalog_complete=True,
            pm_markets=100, matched=50, bound=40, fresh=20, latency_p95=12.0,
            queue_wait_p95=4.0, processing_p95=7.0):
    return build_sample({
        "state": "running",
        "scanner_commit": "runtime-commit",
        "ts": epoch,
        "cycle": int(epoch),
        "update_mode": "event",
        "matching_stats": {
            "pm_markets": pm_markets,
            "matched_pm_markets": matched,
            "unmatched_pm_markets": pm_markets - matched,
            "bound_1win": bound,
            "onewin_catalog_matches": 300,
            "fresh_1win_pairs": fresh,
        },
        "bookmaker_feed_health": {
            "1win": {
                "connected": connected,
                "catalog_ready": True,
                "catalog_matches": 300,
                "bound_pm_markets": bound,
                "subscribed_matches": 80,
                "fresh_open_quotes": 15,
                "stale_open_quotes": 2,
                "odds_snapshots_received": 10,
                "odds_deltas_received": 20,
                "valid_price_updates_received": 30,
                "last_odds_message_age_sec": 0.5,
                "last_price_update_age_sec": 0.2,
                "max_quote_age_sec": 30,
                "open_quote_age_sec": {
                    "sample_count": 15, "p50_sec": 0.5,
                    "p95_sec": 2.0, "max_sec": 5.0,
                },
                "missing_receipt_timestamp_quotes": 0,
                "last_error": error,
            }
        },
        "pm_catalog_complete": catalog_complete,
        "pm_catalog_retained": not catalog_complete,
        "pm_catalog_fetch": {"events": 10, "errors": []},
        "pm_book_health": {
            "token_count": 200,
            "missing_books": 80,
            "missing_timestamps": 0,
            "fresh_books": 100,
            "stale_books": 20,
            "fresh_books_with_asks": 95,
            "book_ttl_sec": 30.0,
            "book_age_sec": {
                "sample_count": 120, "p50_sec": 2.0,
                "p95_sec": 34.0, "max_sec": 60.0,
            },
            "source_event_age_sec": {
                "sample_count": 100, "p50_sec": 1.0,
                "p95_sec": 20.0, "max_sec": 45.0,
            },
        },
        "rolling_event_to_scan_status_latency": {
            "window_sec": 900.0,
            "by_source": {"1win": {"sample_count": 5, "p50_ms": 8.0,
                                     "p95_ms": latency_p95, "max_ms": 20.0}},
        },
        "event_queue_wait_ms_by_source": {"1win": 3.0},
        "rolling_event_queue_wait_latency": {
            "window_sec": 900.0,
            "by_source": {"1win": {"sample_count": 5, "p50_ms": 2.0,
                                     "p95_ms": queue_wait_p95, "max_ms": 6.0}},
        },
        "rolling_dispatch_processing_latency": {
            "window_sec": 900.0,
            "by_source": {"1win": {"sample_count": 5, "p50_ms": 5.0,
                                     "p95_ms": processing_p95, "max_ms": 9.0}},
        },
        "pending_updates_after_drain": 2,
        "runtime_limits": {"freshness": {"onewin_odds_max_age_sec": 30.0}},
        "enabled_bk_sources": ["1win"],
        "signals": 99,
        "bank_usdt": 1000,
    }, sampled_at_epoch=epoch)


class QualityGateBaselineTests(unittest.TestCase):
    def test_summary_calculates_coverage_freshness_and_health_shares(self):
        samples = [
            _sample(1000, pm_markets=100, matched=50, bound=40, fresh=20,
                    latency_p95=12.0, queue_wait_p95=4.0, processing_p95=7.0),
            _sample(1010, connected=False, error="socket reset", catalog_complete=False,
                    pm_markets=120, matched=60, bound=50, fresh=25,
                    latency_p95=25.0, queue_wait_p95=10.0, processing_p95=15.0),
        ]
        records = [
            {"kind": "capture_metadata", "project_commit": "abc123",
             "started_at_utc": "2026-01-01T00:00:00+00:00",
             "requested_duration_sec": 900},
            *samples,
            {"kind": "capture_end", "ended_at_utc": "2026-01-01T00:15:00+00:00",
             "status_read_errors": 1},
        ]
        summary = summarize_records(records)
        self.assertEqual(summary["scanner_commit"], "runtime-commit")
        self.assertEqual(summary["collector_project_commit"], "abc123")
        self.assertEqual(summary["sample_count"], 2)
        self.assertEqual(summary["observed_sample_span_sec"], 10.0)
        self.assertEqual(summary["connected_share_pct"], 50.0)
        self.assertEqual(summary["catalog_complete_share_pct"], 50.0)
        self.assertEqual(summary["feed_error_samples"], 1)
        self.assertEqual(summary["metrics"]["pm_match_coverage_pct"]["p50"], 50.0)
        self.assertEqual(summary["metrics"]["onewin_bound_coverage_pct"]["p50"], 40.0)
        self.assertEqual(summary["metrics"]["fresh_pairs_of_bound_1win_pct"]["p50"], 50.0)
        self.assertEqual(
            summary["rolling_latency_window"]["reported_p95_ms_over_capture"]["1win"]["p95"],
            25.0,
        )
        self.assertEqual(
            summary["rolling_latency_window"]["reported_queue_wait_p95_ms_over_capture"]["1win"]["p50"],
            4.0,
        )
        self.assertEqual(
            summary["rolling_latency_window"]["reported_dispatch_processing_p95_ms_over_capture"]["1win"]["p95"],
            15.0,
        )
        self.assertEqual(summary["metrics"]["onewin_quote_age_p95_sec"]["p50"], 2.0)
        self.assertEqual(summary["metrics"]["pm_book_age_p95_sec"]["p50"], 34.0)
        self.assertEqual(summary["metrics"]["pm_source_event_age_p95_sec"]["p50"], 20.0)
        self.assertNotIn("bank_usdt", summary)
        self.assertNotIn("signals", summary)

    def test_build_sample_keeps_aggregate_diagnostics_only(self):
        sample = _sample(1000)
        self.assertEqual(sample["kind"], "sample")
        self.assertEqual(sample["matching_stats"]["fresh_1win_pairs"], 20)
        self.assertNotIn("bank_usdt", sample)
        self.assertNotIn("signals", sample)
        self.assertEqual(sample["onewin_feed_health"]["fresh_open_quotes"], 15)
        self.assertEqual(sample["onewin_feed_health"]["open_quote_age_sec"]["p95_sec"], 2.0)
        self.assertEqual(sample["pm_book_health"]["stale_books"], 20)
        self.assertEqual(sample["event_queue_wait_ms_by_source"]["1win"], 3.0)
        self.assertEqual(sample["pending_updates_after_drain"], 2)

    def test_capture_reads_local_file_once_per_new_status_and_never_overwrites(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            status_path = root / "scanner_status.json"
            output_path = root / "baseline.jsonl"
            status_path.write_text(json.dumps({
                "state": "running", "ts": 1000, "cycle": 1,
                "matching_stats": {"pm_markets": 1},
            }), encoding="utf-8")
            result = capture(
                status_path, output_path, duration_sec=0.25, interval_sec=0.005,
                project_dir=root,
            )
            records = load_jsonl(output_path)
            self.assertEqual(result["sample_count"], 1)
            self.assertEqual([row["kind"] for row in records], [
                "capture_metadata", "sample", "capture_end",
            ])
            with self.assertRaises(FileExistsError):
                capture(status_path, output_path, duration_sec=0.01,
                        interval_sec=0.005, project_dir=root)

    def test_markdown_labels_latency_as_internal_not_end_to_end(self):
        summary = summarize_records([
            {"kind": "capture_metadata"}, _sample(1000),
            {"kind": "capture_end", "status_read_errors": 0},
        ])
        text = format_markdown(summary)
        self.assertIn("не end-to-end", text)
        self.assertIn("Открытые свежие котировки 1win", text)
        self.assertIn("Queue wait p95", text)
        self.assertIn("Dispatcher processing p95", text)

    def test_summary_requires_at_least_one_sample(self):
        with self.assertRaisesRegex(ValueError, "no scanner status samples"):
            summarize_records([{"kind": "capture_metadata"}])


if __name__ == "__main__":
    unittest.main()
