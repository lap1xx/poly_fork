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


if __name__ == '__main__':
    unittest.main()
