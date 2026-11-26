import json
import tempfile
import unittest
from pathlib import Path

from fork_observer import Observer, active_rows, snapshot_fingerprint, summarize_jsonl


def candidate(phase="live", roi=0.05):
    return {
        "slug": "event-1-map1", "q": "Team A vs Team B", "sport": "CS2",
        "market_type": "MAP1", "phase": phase, "roi": roi,
        "ts": 100.0, "bets": [
            {"source": "Polymarket", "outcome": "Team A", "o": 2.0,
             "pm_best_ask": 0.48, "pm_avg_ask": 0.49, "pm_book_age_sec": 1.2},
            {"source": "1win", "outcome": "Team B", "o": 2.2,
             "odds_age_sec": 0.7},
        ],
    }


class ForkObserverTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.live = self.root / "arbs_live.json"
        self.status = self.root / "scanner_status.json"
        self.log = self.root / "observations.txt"
        self.summary = self.root / "summary.txt"
        self.observer = Observer(self.log, self.summary)

    def tearDown(self):
        self.temp.cleanup()

    def write_inputs(self, now, arbs, drops=None, state="running"):
        self.live.write_text(json.dumps(arbs), encoding="utf-8")
        self.status.write_text(json.dumps({
            "ts": now, "state": state, "recent_signal_drops": drops or [],
        }), encoding="utf-8")

    def events(self):
        return [json.loads(line) for line in self.observer.jsonl_path.read_text(encoding="utf-8").splitlines()]

    def test_active_snapshot_creates_start_and_keeps_analysis_fields(self):
        row = candidate("prematch")
        self.write_inputs(100, {row["slug"]: row})
        self.assertTrue(self.observer.poll(self.live, self.status, now=100))
        events = self.events()
        self.assertEqual([e["event"] for e in events], ["fork_start", "fork_snapshot"])
        self.assertEqual(events[0]["phase"], "prematch")
        self.assertEqual(events[1]["snapshot"]["bets"][0]["pm_best_ask"], 0.48)
        self.assertEqual(events[1]["snapshot"]["bets"][1]["odds_age_sec"], 0.7)
        readable = self.log.read_text(encoding="utf-8")
        self.assertIn("НАЙДЕНА ВИЛКА", readable)
        self.assertIn("Polymarket: исход Team A", readable)
        self.assertIn("БК 1win: исход Team B", readable)

    def test_calculation_timestamp_alone_does_not_duplicate_quote_snapshot(self):
        row = candidate()
        self.assertEqual(snapshot_fingerprint(row), snapshot_fingerprint({**row, "ts": 101}))

    def test_closes_after_two_missing_observations_and_records_drop_reason(self):
        row = candidate()
        self.write_inputs(100, {row["slug"]: row})
        self.observer.poll(self.live, self.status, now=100)
        self.write_inputs(110, {}, [{"slug": row["slug"], "reason": "pm_book_missing", "dropped_at": 109}])
        self.observer.poll(self.live, self.status, now=110)
        self.observer.poll(self.live, self.status, now=111)
        end = [event for event in self.events() if event["event"] == "fork_end"]
        self.assertEqual(len(end), 1)
        self.assertEqual(end[0]["reason"], "pm_book_missing")
        self.assertAlmostEqual(end[0]["duration_sec"], 9.0)
        self.assertEqual(end[0]["duration_basis"], "observed_interval_estimate")

    def test_stale_status_does_not_close_an_episode(self):
        row = candidate()
        self.write_inputs(100, {row["slug"]: row})
        self.observer.poll(self.live, self.status, now=100)
        self.write_inputs(101, {})
        self.observer.poll(self.live, self.status, now=120)
        self.observer.poll(self.live, self.status, now=121)
        self.assertFalse(any(event["event"] == "fork_end" for event in self.events()))
        self.assertEqual(len(self.observer.episodes), 1)

    def test_summary_counts_phase_durations_and_drop_causes(self):
        self.write_inputs(100, {"event-1": candidate("prematch")})
        self.observer.poll(self.live, self.status, now=100)
        self.write_inputs(105, {}, [{"slug": "event-1-map1", "reason": "below_roi_threshold", "dropped_at": 104}])
        self.observer.poll(self.live, self.status, now=105)
        self.observer.poll(self.live, self.status, now=106)
        summary = summarize_jsonl(self.observer.jsonl_path)
        self.assertIn("prematch: 1", summary)
        self.assertIn("below_roi_threshold: 1", summary)
        self.assertIn("4.0 сек", summary)

    def test_inactive_snapshots_are_not_counted_as_active(self):
        row = candidate()
        row["inactive_snapshot"] = True
        self.assertEqual(active_rows({row["slug"]: row}), {})


if __name__ == "__main__":
    unittest.main()
