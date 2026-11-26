import unittest
from unittest.mock import patch

import pm_market_data as pm
import web_app


class PMBookSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.token = 'test-live-pm-token'
        with pm.LOCK:
            self.previous = pm.BOOKS.get(self.token)
            pm.BOOKS[self.token] = {
                'ts': 1000.0, 'source_ts': 999.5,
                'asks': [(0.4, 2.0), (0.5, 3.0), (0.6, 4.0)],
                'bids': [(0.3, 1.0), (0.2, 2.0)],
            }

    def tearDown(self):
        with pm.LOCK:
            if self.previous is None:
                pm.BOOKS.pop(self.token, None)
            else:
                pm.BOOKS[self.token] = self.previous

    def test_snapshot_is_bounded_and_detached(self):
        result = pm.snapshot_pm_book(self.token, now=1001, max_ask_levels=2, max_bid_levels=1)
        self.assertEqual(result['token'], self.token)
        self.assertEqual(result['source_ts'], 999.5)
        self.assertEqual(result['received_at'], 1000)
        self.assertEqual(result['total_ask_levels'], 3)
        self.assertTrue(result['asks_truncated'])
        self.assertEqual(result['asks'], [{'price': 0.4, 'size': 2.0}, {'price': 0.5, 'size': 3.0}])
        self.assertEqual(result['bids'], [{'price': 0.3, 'size': 1.0}])
        result['asks'][0]['size'] = 99
        with pm.LOCK:
            self.assertEqual(pm.BOOKS[self.token]['asks'][0][1], 2.0)

    def test_missing_stale_future_empty_and_invalid_books_fail_closed(self):
        self.assertIsNone(pm.snapshot_pm_book('missing-token', now=1000))
        self.assertIsNone(pm.snapshot_pm_book(self.token, now=999.9))
        self.assertIsNotNone(pm.snapshot_pm_book(self.token, now=1000 + pm.BOOK_TTL))
        self.assertIsNone(pm.snapshot_pm_book(self.token, now=1000 + pm.BOOK_TTL + .01))
        with pm.LOCK:
            pm.BOOKS[self.token]['asks'] = []
        self.assertIsNone(pm.snapshot_pm_book(self.token, now=1000))
        with pm.LOCK:
            pm.BOOKS[self.token]['asks'] = [(0.5, 1), (0.4, 2)]
        self.assertIsNone(pm.snapshot_pm_book(self.token, now=1000))
        with pm.LOCK:
            pm.BOOKS[self.token]['asks'] = [(0.5, float('nan'))]
        self.assertIsNone(pm.snapshot_pm_book(self.token, now=1000))

    def test_price_delta_is_in_new_snapshot_without_rest_or_network(self):
        updates = []
        feed = pm.Feed([self.token], on_update=updates.append,
                       schedule_refresh=lambda *args: self.fail('unexpected REST refresh'))
        with patch.object(pm.time, 'time', return_value=1001.0):
            feed._a({'event_type': 'price_change', 'timestamp': 1001.0, 'price_changes': [
                {'asset_id': self.token, 'side': 'SELL', 'price': '0.4', 'size': '1.5'},
            ]})
        result = pm.snapshot_pm_book(self.token, now=1001)
        self.assertEqual(result['asks'][0], {'price': 0.4, 'size': 1.5})
        self.assertEqual(result['received_at'], 1001)
        self.assertEqual(updates, [self.token])

    def test_backend_preserves_additive_book_field(self):
        snapshot = pm.snapshot_pm_book(self.token, now=1001)
        row = {'bets': [{'source': 'Polymarket', 'token': self.token}],
               'pm_book_snapshot': snapshot}
        with patch.object(web_app, '_load_config', return_value={'safe_fork_threshold': 0.45}), \
             patch.object(web_app, 'bk_adapter', None), \
             patch.object(web_app.time, 'time', return_value=1001):
            result = web_app._build_enriched({'test-slug': row})
        self.assertEqual(result['test-slug']['pm_book_snapshot'], snapshot)


if __name__ == '__main__':
    unittest.main()
