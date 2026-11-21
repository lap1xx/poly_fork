import asyncio
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import web_app


class PanelStatsDateTests(unittest.TestCase):
    def test_default_daily_uses_utc_date(self):
        fixed_utc = time.struct_time((2030, 1, 2, 0, 10, 0, 2, 2, 0))
        with TemporaryDirectory() as tmp:
            with patch.object(web_app, 'TRADES_FILE', Path(tmp) / 'trades.jsonl'), \
                 patch.object(web_app.time, 'gmtime', return_value=fixed_utc):
                result = asyncio.run(web_app.api_daily())
        self.assertEqual(result['date'], '2030-01-02')
        self.assertEqual(result['count'], 0)

    def test_weekly_uses_utc_calendar_day(self):
        class FakeDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                if tz is None:
                    # A local server in a western timezone is still on Jan 1.
                    return cls(2030, 1, 1, 16, 10)
                return cls(2030, 1, 2, 0, 10, tzinfo=timezone.utc)

        with TemporaryDirectory() as tmp:
            with patch.object(web_app, 'TRADES_FILE', Path(tmp) / 'trades.jsonl'), \
                 patch('datetime.datetime', FakeDateTime):
                result = asyncio.run(web_app.api_weekly())
        self.assertEqual(len(result['days']), 7)
        self.assertEqual(result['days'][-1]['date'], '2030-01-02')
        self.assertEqual(result['days'][0]['date'], '2029-12-27')


class SourceFreshnessTests(unittest.TestCase):
    def test_bookmaker_quote_age_marks_fresh_snapshot_stale(self):
        raw = {
            "cs2-team-alpha-team-bravo-winner": {
                "ts": time.time(),
                "bk_odds_age_sec": 7.0,
                "bets": [],
            }
        }
        with patch.object(web_app, "_load_config", return_value={"safe_fork_threshold": 0.03}), \
             patch.object(web_app, "bk_adapter", None), \
             patch.object(web_app, "DATA_STALE_SEC", 5.0):
            result = web_app._build_enriched(raw)
        item = result["cs2-team-alpha-team-bravo-winner"]
        self.assertEqual(item['source_state'], 'stale')
        self.assertGreaterEqual(item['source_age_sec'], 7.0)

    def test_phase_is_tri_state_and_legacy_false_does_not_become_prematch(self):
        raw = {
            "legacy-no-phase": {"ts": time.time(), "is_live": False, "bets": []},
            "confirmed-live": {"ts": time.time(), "phase": "live", "bets": []},
            "confirmed-prematch": {"ts": time.time(), "phase": "prematch", "bets": []},
            "unknown": {"ts": time.time(), "phase": "unknown", "is_live": False, "bets": []},
        }
        with patch.object(web_app, "_load_config", return_value={"safe_fork_threshold": 0.03}), \
             patch.object(web_app, "bk_adapter", None):
            result = web_app._build_enriched(raw)
        self.assertEqual(result["legacy-no-phase"]["phase"], "unknown")
        self.assertFalse(result["legacy-no-phase"]["is_live"])
        self.assertEqual(result["confirmed-live"]["phase"], "live")
        self.assertTrue(result["confirmed-live"]["is_live"])
        self.assertEqual(result["confirmed-prematch"]["phase"], "prematch")
        self.assertEqual(result["unknown"]["phase"], "unknown")


class CrossSourcePairValidationTests(unittest.TestCase):
    @staticmethod
    def trade_payload(books=("Polymarket", "1win")):
        return {
            "slug": "cs2-alpha-bravo-winner",
            "q": "Alpha vs Bravo",
            "bank": 100.0,
            "profit": 4.0,
            "roi": 0.04,
            "payout": 104.0,
            "rate": 1.0,
            "currency": "USDT",
            "legs": [
                {
                    "book": books[0], "outcome": "Alpha",
                    "price_usd": 0.45, "amount_usdt": 50.0,
                },
                {
                    "book": books[1], "outcome": "Bravo",
                    "odds": 2.3, "stake_usdt": 50.0,
                },
            ],
        }

    def test_manual_trade_record_accepts_only_pm_plus_one_bookmaker(self):
        with TemporaryDirectory() as tmp:
            trade_file = Path(tmp) / "trades.jsonl"
            with patch.object(web_app, "TRADES_FILE", trade_file):
                result = asyncio.run(web_app.api_trade(self.trade_payload()))
        self.assertTrue(result["ok"])

    def test_manual_trade_record_rejects_two_bookmakers(self):
        with TemporaryDirectory() as tmp:
            trade_file = Path(tmp) / "trades.jsonl"
            with patch.object(web_app, "TRADES_FILE", trade_file):
                result = asyncio.run(
                    web_app.api_trade(self.trade_payload(("1win", "BetBoom")))
                )
        self.assertEqual(result.status_code, 422)
        self.assertFalse(trade_file.exists())

    def test_scanner_arb_rejects_bookmaker_source_metadata_mismatch(self):
        arb = {
            "bk_source": "1win",
            "bets": [
                {"source": "Polymarket", "outcome": "Alpha", "token": "pm-1", "o": 2.1},
                {"source": "BetBoom", "outcome": "Bravo", "o": 2.2},
            ],
        }
        self.assertFalse(web_app._is_valid_scanner_arb(arb))


if __name__ == '__main__':
    unittest.main()
