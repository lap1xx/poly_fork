"""Offline checks for PM receipt age versus unchanged best-ask age."""
import unittest
from unittest.mock import patch

import pm_market_data as pm
import polyarb_v4 as scanner
from opportunity import normalize_scanner_snapshot


class PMBookAgeSemanticsTests(unittest.TestCase):
    def setUp(self):
        self.tokens = ['pm-age-test-a', 'pm-age-test-b']
        with pm.LOCK:
            self.previous = {token: pm.BOOKS.get(token) for token in self.tokens}
            for token in self.tokens:
                pm.BOOKS[token] = {
                    'ts': 999.0, 'last_change': 942.0,
                    'asks': [(0.5, 1000.0)], 'bids': [(0.49, 1000.0)],
                }

    def tearDown(self):
        with pm.LOCK:
            for token, value in self.previous.items():
                if value is None:
                    pm.BOOKS.pop(token, None)
                else:
                    pm.BOOKS[token] = value

    def build(self):
        market = {'o': ['A', 'B'], 't': self.tokens, 'condition_id': 'unit-test'}
        with patch.object(scanner, '_maybe_refresh_stale', return_value=False), \
             patch.object(scanner, 'fetch_pm_fee_schedule', return_value={
                 'fees_enabled': False, 'rate': 0.0, 'exponent': 1.0,
                 'source': 'clob_market_info',
             }):
            return scanner.build_arb_candidates(
                market, {'A': {'o': 3.0, 'age_sec': 1.0},
                         'B': {'o': 3.0, 'age_sec': 1.0}},
                0.0, bk_source='1win',
            )

    def normalized(self, bets, *, snapshot_time=1000.0):
        calculation = scanner.compute(bets, 40.0)
        payload = {
            'type': 'scanner.snapshot', 'schema_version': 1,
            'scanner_id': 'polyarb_v4', 'session_id': 'age-test', 'seq': 1,
            'sent_at': snapshot_time,
            'arbs': {'event': {
                'slug': 'event', 'q': 'A vs B', 'sport': 'cs2',
                'market_type': 'winner', 'phase': 'live', 'currency': 'USDT',
                'roi': calculation['roi'], 'stakes': calculation['stakes'],
                'profit': calculation['profit'], 'ts': snapshot_time,
                'bk_source': '1win', 'bets': scanner._serialize_candidate_bets(
                    bets, now=snapshot_time),
            }},
        }
        return normalize_scanner_snapshot(payload, now_epoch=snapshot_time)

    def test_fresh_book_and_unchanged_ask_have_independent_ages(self):
        with patch.object(pm.time, 'time', return_value=1000.0):
            pair = self.build()[0]
        leg = pair[0]
        self.assertEqual(leg['pm_age'], 58.0)
        self.assertEqual(leg['pm_book_age_sec'], 1.0)
        self.assertEqual(leg['pm_book_age_source'], 'local_receipt')
        serialized = scanner._serialize_candidate_bets(pair, now=1000.0)[0]
        self.assertEqual(serialized['pm_book_age_sec'], 1.0)
        result = self.normalized(pair)
        self.assertEqual(result['rejected'], [])
        self.assertEqual(result['opportunities'][0]['status'], 'active')
        self.assertEqual(result['opportunities'][0]['legs'][0]['age_ms'], 1000.0)

    def test_old_receipt_is_not_refreshed_by_new_best_ask_change(self):
        with pm.LOCK:
            for token in self.tokens:
                pm.BOOKS[token]['ts'] = 969.0
                pm.BOOKS[token]['last_change'] = 999.0
        with patch.object(pm.time, 'time', return_value=1000.0):
            self.assertEqual(pm.pm_age_sec(self.tokens[0]), 1.0)
            self.assertEqual(pm.best_ask(self.tokens[0]), 0.0)
            self.assertIsNone(pm.walk_book_with_fee(self.tokens[0], 50, 0, 1))
            self.assertEqual(self.build(), [])

    def test_book_update_during_refinement_does_not_rejuvenate_earlier_quote(self):
        with patch.object(pm.time, 'time', return_value=1000.0):
            pair = self.build()[0]
        with pm.LOCK:
            pm.BOOKS[self.tokens[0]]['ts'] = 1001.0
            pm.BOOKS[self.tokens[0]]['asks'] = [(0.5, 1500.0)]
        with patch.object(pm.time, 'time', return_value=1002.0):
            result = scanner.refine_stakes(pair, scanner.compute(pair, 40.0), 1.0)
        self.assertIsNotNone(result)
        self.assertEqual(pair[0]['pm_book_received_at'], 999.0)
        serialized = scanner._serialize_candidate_bets(pair, now=1002.0)[0]
        self.assertEqual(serialized['pm_book_age_sec'], 3.0)
        self.assertEqual(serialized['pm_book_received_at'], 999.0)

    def test_top_ask_and_depth_quote_use_same_book_version(self):
        original = scanner.best_ask

        def update_after_read(token):
            old_ask = original(token)
            if token == self.tokens[0]:
                with pm.LOCK:
                    pm.BOOKS[token]['asks'] = [(0.55, 1000.0)]
                    pm.BOOKS[token]['ts'] = 1000.0
            return old_ask

        with patch.object(pm.time, 'time', return_value=1000.0), \
             patch.object(scanner, 'best_ask', side_effect=update_after_read):
            pair = self.build()[0]
        self.assertEqual(pair[0]['pm_best_ask'], 0.55)
        self.assertAlmostEqual(pair[0]['pm_avg_ask'], 0.55)
        self.assertEqual(pair[0]['pm_book_received_at'], 1000.0)

    def test_missing_receipt_age_fails_closed_without_legacy_fallback(self):
        with patch.object(pm.time, 'time', return_value=1000.0):
            pair = self.build()[0]
        pair[0]['pm_book_received_at'] = None
        pair[0]['pm_age'] = 1.0
        self.assertIsNone(scanner._serialize_candidate_bets(pair, now=1000.0)[0]['pm_book_age_sec'])
        result = self.normalized(pair)
        self.assertEqual(result['opportunities'], [])
        self.assertIn('quote age is missing', result['rejected'][0]['reason'])

    def test_missing_earlier_receipt_not_revived_by_refinement(self):
        with patch.object(pm.time, 'time', return_value=1000.0):
            pair = self.build()[0]
        pair[0]['pm_book_received_at'] = None
        with patch.object(pm.time, 'time', return_value=1001.0):
            result = scanner.refine_stakes(pair, scanner.compute(pair, 40.0), 1.0)
        self.assertIsNotNone(result)
        self.assertIsNone(pair[0]['pm_book_received_at'])
        self.assertIsNone(scanner._serialize_candidate_bets(pair, now=1001.0)[0]['pm_book_age_sec'])

    def test_future_receipt_during_refinement_makes_age_unknown(self):
        with patch.object(pm.time, 'time', return_value=1000.0):
            pair = self.build()[0]
        with pm.LOCK:
            pm.BOOKS[self.tokens[0]]['ts'] = 1002.0
        with patch.object(pm.time, 'time', return_value=1001.0):
            result = scanner.refine_stakes(pair, scanner.compute(pair, 40.0), 1.0)
        self.assertIsNotNone(result)
        self.assertIsNone(pair[0]['pm_book_received_at'])
        self.assertIsNone(scanner._serialize_candidate_bets(pair, now=1001.0)[0]['pm_book_age_sec'])

    def test_missing_future_and_invalid_receipt_timestamps_do_not_become_fresh(self):
        with patch.object(pm.time, 'time', return_value=1000.0):
            pair = self.build()[0]
        for timestamp in (None, 0, 1001, float('nan'), 'bad'):
            with self.subTest(timestamp=timestamp):
                pair[0]['pm_book_received_at'] = timestamp
                leg = scanner._serialize_candidate_bets(pair, now=1000.0)[0]
                self.assertIsNone(leg['pm_book_age_sec'])
                self.assertEqual(leg['pm_book_age_source'], 'local_receipt')

    def test_drop_diagnostic_marks_receipt_age_and_best_ask_age(self):
        with patch.object(pm.time, 'time', return_value=1000.0):
            pair = self.build()[0]
        snapshot = {'slug': 'event', 'ts': 1000.0,
                    'bets': scanner._serialize_candidate_bets(pair, now=1000.0)}
        quote = scanner._make_signal_drop_diagnostic(snapshot, 'below_roi_threshold', 1001.0)['pm_quote']
        self.assertEqual(quote['book_age_source'], 'local_receipt')
        self.assertEqual(quote['book_age_sec'], 1.0)
        self.assertEqual(quote['best_ask_change_age_sec'], 58.0)


if __name__ == '__main__':
    unittest.main()
