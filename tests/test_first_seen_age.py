"""Regression tests for persisted signal-age timestamps."""
from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import web_app


class FirstSeenPersistenceTests(unittest.TestCase):
    def test_discards_stale_future_and_invalid_persisted_timestamps(self):
        now = time.time()
        recent_ts = now - 25.0
        persisted = {
            "recent-market": recent_ts,
            "old-market": now - web_app.FIRST_SEEN_TTL - 1.0,
            "future-market": now + 60.0,
            "invalid-market": "not-a-timestamp",
            "non-finite-market": "nan",
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            first_seen_path = Path(temp_dir) / "arb_first_seen.json"
            first_seen_path.write_text(json.dumps(persisted), encoding="utf-8")
            with patch.object(web_app, "FIRST_SEEN_FILE", first_seen_path), patch.object(
                web_app, "_first_seen", {"previous-process-entry": now - 10.0}
            ):
                web_app._load_first_seen()
                self.assertEqual(web_app._first_seen, {"recent-market": recent_ts})


if __name__ == "__main__":
    unittest.main()
