import json
import threading
import time
import unittest
from unittest.mock import patch

import polyarb_v4
import scanner_publisher
import web_app


class SnapshotBudgetTests(unittest.TestCase):
    def payload(self):
        book = {'token': 'token-a', 'asks': [{'price': .4, 'size': 2}] * 30}
        row = {'slug': 'one', 'roi': .06, 'bets': [
            {'source': 'Polymarket', 'token': 'token-a', 'outcome': 'A'},
            {'source': '1win', 'outcome': 'B'},
        ], 'pm_book_snapshot': book}
        return {'type': 'scanner.snapshot', 'arbs': {'one': row},
                'scan_status': {'pinned_signal_snapshots': [dict(row)]}}

    def test_overflow_omits_only_books_without_mutating_source(self):
        payload = self.payload()
        original = json.loads(json.dumps(payload))
        plain, plain_size, _ = scanner_publisher._serialize_snapshot_with_book_fallback(payload, 10_000)
        compact, compact_size, omitted = scanner_publisher._serialize_snapshot_with_book_fallback(payload, 800)
        result = json.loads(compact)
        self.assertGreater(plain_size, 800)
        self.assertLessEqual(compact_size, 800)
        self.assertEqual(omitted, 2)
        self.assertEqual(result['scan_status']['pm_book_payload_omitted'], 2)
        self.assertNotIn('pm_book_snapshot', result['arbs']['one'])
        self.assertNotIn('pm_book_snapshot', result['scan_status']['pinned_signal_snapshots'][0])
        self.assertEqual(result['arbs']['one']['bets'], original['arbs']['one']['bets'])
        self.assertEqual(result['arbs']['one']['roi'], .06)
        self.assertEqual(payload, original)
        self.assertIn('pm_book_snapshot', json.loads(plain)['arbs']['one'])

    def test_existing_payload_that_cannot_fit_is_not_truncated(self):
        payload = self.payload()
        payload['arbs']['one']['q'] = 'x' * 2000
        text, size, _ = scanner_publisher._serialize_snapshot_with_book_fallback(payload, 800)
        self.assertGreater(size, 800)
        self.assertEqual(json.loads(text)['arbs']['one']['q'], 'x' * 2000)

    def test_nonfinite_values_remain_rejected(self):
        payload = self.payload()
        payload['arbs']['one']['roi'] = float('nan')
        with self.assertRaises(ValueError):
            scanner_publisher._serialize_snapshot_with_book_fallback(payload, 8_000_000)

    def test_pinned_snapshot_does_not_copy_live_book(self):
        payload = self.payload()
        row = payload['arbs']['one']
        pins = {}
        self.assertTrue(polyarb_v4._pin_signal_snapshot(pins, row, now=1000.0))
        frozen = next(iter(pins.values()))
        self.assertNotIn('pm_book_snapshot', frozen)
        self.assertIn('pm_book_snapshot', row)
        self.assertEqual(frozen['bets'], row['bets'])
        self.assertEqual(web_app.SCANNER_WS_MAX_BYTES, scanner_publisher.SCANNER_WS_MAX_BYTES)

    def test_oversize_does_not_send_or_reconnect_and_next_small_snapshot_sends(self):
        class Socket:
            def __init__(self):
                self.sent = []
                self.closed = threading.Event()
            def settimeout(self, timeout):
                pass
            def send(self, text):
                self.sent.append(text)
            def recv(self):
                self.closed.wait(5)
            def close(self):
                self.closed.set()
        class Transport:
            def __init__(self):
                self.socket = Socket()
                self.connections = 0
            def create_connection(self, *args, **kwargs):
                self.connections += 1
                return self.socket
        transport = Transport()
        processed = threading.Event()
        serialize = scanner_publisher._serialize_snapshot_with_book_fallback
        def observe(payload, limit):
            result = serialize(payload, limit)
            if 'large' in payload['arbs']:
                processed.set()
            return result
        with patch.object(scanner_publisher, 'SCANNER_WS_MAX_BYTES', 512), \
             patch.object(scanner_publisher, '_serialize_snapshot_with_book_fallback', side_effect=observe):
            publisher = scanner_publisher.ScannerPublisher('ws://test', 'secret', websocket_module=transport)
            try:
                publisher.publish({'large': {'description': 'x' * 3000}}, {})
                self.assertTrue(processed.wait(2.0), 'oversize snapshot was not processed')
                self.assertTrue(all(len(text.encode('utf-8')) <= 512 for text in transport.socket.sent))
                publisher.publish({'small': {'roi': .1}}, {})
                until = time.monotonic() + 2
                while time.monotonic() < until:
                    if any('small' in json.loads(text)['arbs'] for text in transport.socket.sent):
                        break
                    time.sleep(.01)
                else:
                    self.fail('small recovery snapshot was not sent')
                self.assertEqual(transport.connections, 1)
                self.assertTrue(all('large' not in json.loads(text)['arbs'] for text in transport.socket.sent))
            finally:
                publisher.close()


if __name__ == '__main__':
    unittest.main()
