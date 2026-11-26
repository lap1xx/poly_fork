import copy
import io
import json
import math
import shutil
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import TestCase
from unittest.mock import Mock, patch

import bk_router
import onewin_bridge
import pm_catalog
import polyarb_v4
import web_app
from event_dispatch import DirtyUpdateQueue, resolve_dirty_slugs
from event_phase import capture_pm_phase_evidence, resolve_event_phase

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = json.loads((ROOT / 'tests/fixtures/event_phase_baks_20261126.json').read_text(encoding='utf-8'))


def timestamp(text):
    return datetime.fromisoformat(text.replace('Z', '+00:00'))


class PhaseEvidenceTests(TestCase):
    def setUp(self):
        self.now = timestamp('2026-11-26T15:12:54Z')
        self.market = {
            'slug': FIXTURE['market_slug'], 'phase': 'unknown', '_phase_classified': True,
            '_pm_phase_evidence': capture_pm_phase_evidence(
                FIXTURE['polymarket']['market'], FIXTURE['polymarket']['event'],
                received_at=timestamp(FIXTURE['polymarket']['observed_at']).timestamp()),
        }
        ow = FIXTURE['onewin']
        self.ow = {'service': ow['service'], 'serviceV2': ow['serviceV2'],
                   'startAt': ow['startAt'], 'catalog_at': timestamp(ow['observed_at']).timestamp()}

    def phase(self, market=None, ow=None, now=None):
        return resolve_event_phase(self.market if market is None else market,
                                   self.ow if ow is None else ow, now=now or self.now)

    def test_observed_baks_disagreement_uses_onewin_not_a_fake_joint_confirmation(self):
        result = self.phase()
        self.assertEqual(result, {'phase': 'prematch', 'source': '1win',
                                  'reason': 'onewin_prematch', 'start_difference_sec': 300.0})
        # The later 1win request cannot retrospectively prove the earlier screenshot.
        result = self.phase(now=timestamp('2026-11-26T15:10:40Z'))
        self.assertEqual(result['phase'], 'unknown')

    def test_start_is_strict_boundary_and_does_not_guess_live(self):
        for offset, expected in [(-0.001, 'prematch'), (0, 'unknown'), (0.001, 'unknown')]:
            now = timestamp('2026-11-26T15:15:00Z') + timedelta(seconds=offset)
            market, ow = copy.deepcopy(self.market), dict(self.ow)
            market['_pm_phase_evidence']['received_at'] = now.timestamp() - 1
            ow['catalog_at'] = now.timestamp() - 1
            self.assertEqual(self.phase(market, ow, now)['phase'], expected)

    def test_pm_conflict_terminal_invalid_and_unrecognized_do_not_allow_fallback(self):
        for fragment in ({'live': True}, {'status': 'live'}, {'closed': True},
                         {'active': False}, {'ended': True}, {'status': 'cancelled'},
                         {'status': 'postponed'}, {'status': 'mystery'},
                         {'live': 'not-a-boolean'}, {'startTime': 'not-a-date'}):
            with self.subTest(fragment=fragment):
                market = copy.deepcopy(self.market)
                market['_pm_phase_evidence']['market'].update(fragment)
                self.assertEqual(self.phase(market)['phase'], 'unknown')

    def test_explicit_live_pm_conflicts_with_prematch_onewin(self):
        market = copy.deepcopy(self.market)
        market['_pm_phase_evidence']['event']['live'] = True
        result = self.phase(market)
        self.assertEqual((result['phase'], result['source'], result['reason']),
                         ('unknown', 'conflict', 'source_phase_conflict'))
        for start in (None, 'invalid', self.now.timestamp() - 1):
            with self.subTest(start=start):
                result = self.phase(market, dict(self.ow, startAt=start))
                self.assertEqual(result['reason'], 'source_phase_conflict')
        # A newly received WS state can veto PM, but cannot rejuvenate the
        # independent, stale catalog start time for a prematch fallback.
        ws = dict(self.ow, catalog_at=self.now.timestamp() - 211,
                  ws_service='PREMATCH', ws_service_at=self.now.timestamp())
        self.assertEqual(self.phase(market, ws)['source'], 'conflict')
        self.assertEqual(self.phase(ow=ws)['phase'], 'unknown')

    def test_pm_prematch_conflicts_with_explicit_onewin_live_or_elapsed_start(self):
        market = copy.deepcopy(self.market)
        market['_pm_phase_evidence']['event']['startTime'] = '2026-11-26T16:00:00Z'
        market['_pm_phase_evidence']['market']['eventStartTime'] = '2026-11-26T16:00:00Z'
        live = dict(self.ow, service='LIVE', serviceV2='live')
        self.assertEqual(self.phase(market, live)['source'], 'conflict')
        expired = dict(self.ow, startAt=int(self.now.timestamp()))
        self.assertEqual(self.phase(market, expired)['reason'], 'onewin_start_passed')

    def test_invalid_missing_stale_and_future_receipts_do_not_enable_fallback(self):
        for receipt in (None, '', False, float('nan'), float('inf'), -1,
                        self.now.timestamp() + 0.1, self.now.timestamp() - 210.01):
            with self.subTest(receipt=receipt):
                self.assertEqual(self.phase(ow=dict(self.ow, catalog_at=receipt))['phase'], 'unknown')
        self.assertEqual(self.phase(ow=dict(self.ow, catalog_at=self.now.timestamp() - 210))['phase'], 'prematch')
        for receipt in (None, False, float('nan'), self.now.timestamp() + 1, self.now.timestamp() - 120.01):
            with self.subTest(pm_receipt=receipt):
                market = copy.deepcopy(self.market)
                market['_pm_phase_evidence']['received_at'] = receipt
                self.assertEqual(self.phase(market)['reason'], 'pm_evidence_stale')

    def test_missing_invalid_or_conflicting_onewin_fields_are_never_guessed(self):
        for changes in ({'startAt': None}, {'startAt': True}, {'startAt': 'bad'},
                        {'startAt': math.inf}, {'service': None, 'serviceV2': None},
                        {'service': 'unknown', 'serviceV2': 'prematch'},
                        {'service': 'LIVE', 'serviceV2': 'prematch'}):
            with self.subTest(changes=changes):
                self.assertEqual(self.phase(ow=dict(self.ow, **changes))['phase'], 'unknown')
        iso = dict(self.ow, startAt='2026-11-26T15:15:00Z')
        milliseconds = dict(self.ow, startAt=1795706100000)
        self.assertEqual(self.phase(ow=iso)['phase'], 'prematch')
        self.assertEqual(self.phase(ow=milliseconds)['phase'], 'prematch')

    def test_later_ws_service_supersedes_older_catalog_without_rejuvenating_start(self):
        ws = dict(self.ow, ws_service='LIVE', ws_service_at=self.now.timestamp())
        self.assertEqual(self.phase(ow=ws)['phase'], 'unknown')
        ws['ws_service'] = 'PREMATCH'
        self.assertEqual(self.phase(ow=ws)['phase'], 'prematch')
        ws['ws_service'] = None
        self.assertEqual(self.phase(ow=ws)['phase'], 'unknown')
        ws['catalog_at'] = self.now.timestamp() - 211
        self.assertEqual(self.phase(ow=ws)['phase'], 'unknown')
        ws.update(catalog_at=self.now.timestamp() - 1, ws_service='LIVE',
                  ws_service_at=self.now.timestamp() - 220)
        self.assertEqual(self.phase(ow=ws)['phase'], 'prematch')

    def test_legacy_market_stays_compatible_and_cannot_use_unknown_as_evidence(self):
        self.assertEqual(self.phase({'phase': 'unknown', '_phase_classified': True})['phase'], 'unknown')
        self.assertEqual(self.phase({'phase': 'live', '_phase_classified': True})['phase'], 'live')

    def test_pm_scheduled_start_rechecks_without_waiting_for_catalog_refresh(self):
        market = copy.deepcopy(self.market)
        market['_pm_phase_evidence']['event']['startTime'] = '2026-11-26T15:13:00Z'
        market['_pm_phase_evidence']['market']['eventStartTime'] = '2026-11-26T15:13:00Z'
        self.assertEqual(resolve_event_phase(market, now=self.now)['phase'], 'prematch')
        self.assertEqual(resolve_event_phase(market, now=self.now + timedelta(seconds=6))['phase'], 'unknown')

    def test_receipt_only_refresh_does_not_dirty_every_market(self):
        refreshed = copy.deepcopy(self.market)
        refreshed['_pm_phase_evidence']['received_at'] += 20
        self.assertFalse(polyarb_v4._market_content_changed(self.market, refreshed))
        refreshed['_pm_phase_evidence']['event']['live'] = True
        self.assertTrue(polyarb_v4._market_content_changed(self.market, refreshed))


class PhaseBridgeTests(TestCase):
    def setUp(self):
        self.stream = onewin_bridge.OneWinStream()
        self.match = {'id': 123, 'sportId': 142, 'service': 'PREMATCH', 'serviceV2': 'prematch',
                      'startAt': 2_000_000_300, 'homeTeam': {'name': 'Alpha'},
                      'awayTeam': {'name': 'Beta'}}
        with patch.object(onewin_bridge.time, 'time', return_value=2_000_000_000):
            self.stream._store_matches([self.match])
        self.slug = 'cs2-alpha-beta-2033-05-18'
        self.stream.bindings[self.slug] = {'match_id': 123}
        self.stream._connected.set()
        self.router = bk_router.BKRouter(use_bb=False, use_onewin=False)
        self.router.use_onewin = True
        self.router._onewin_stream = self.stream
        self.router.bindings[self.slug] = ('1win', {'match_id': 123})

    def test_lookup_requires_exact_existing_binding_and_connection_and_never_starts_feed(self):
        with patch.object(onewin_bridge, 'get_global_stream', side_effect=AssertionError('must not start')):
            self.assertEqual(self.router.onewin_phase_evidence(self.slug)['service'], 'PREMATCH')
            self.assertIsNone(self.router.onewin_phase_evidence('another'))
            self.router.bindings[self.slug] = ('1win', {'match_id': 456})
            self.assertIsNone(self.router.onewin_phase_evidence(self.slug))
            self.router.bindings[self.slug] = ('BetBoom', {'match_id': 123})
            self.assertIsNone(self.router.onewin_phase_evidence(self.slug))
            self.router.bindings[self.slug] = ('1win', {'match_id': 123})
            self.router._active_sources[self.slug] = 'BetBoom'
            self.assertIsNone(self.router.onewin_phase_evidence(self.slug))
            self.router._active_sources.clear()
            self.router.use_onewin = False
            self.assertIsNone(self.router.onewin_phase_evidence(self.slug))
            self.router.use_onewin = True
            self.stream._connected.clear()
            self.assertIsNone(self.router.onewin_phase_evidence(self.slug))
            self.stream._connected.set()
            self.router._onewin_stream = None
            self.assertIsNone(self.router.onewin_phase_evidence(self.slug))

    def test_info_only_status_wakes_dispatch_but_other_messages_do_not_refresh_status(self):
        callbacks = []
        self.router.set_adapter_update_callback(lambda *args: callbacks.append(args))
        with patch.object(onewin_bridge.time, 'time', return_value=2_000_000_005):
            self.stream._on_match_info({'matchId': 123, 'service': 'LIVE'})
        evidence = self.router.onewin_phase_evidence(self.slug)
        self.assertEqual((evidence['ws_service'], evidence['ws_service_at']), ('LIVE', 2_000_000_005))
        self.assertEqual(callbacks[0][:2], ('1win', '123'))
        with patch.object(onewin_bridge.time, 'time', return_value=2_000_000_006):
            self.stream._on_match_info({'matchId': 123, 'scoreBoard': {}})
            self.stream._on_match_odds({'matchId': 123, 'oddsGroups': []})
        self.assertEqual(self.router.onewin_phase_evidence(self.slug)['ws_service_at'], 2_000_000_005)
        self.assertEqual(len(callbacks), 2)  # one status, one odds; no callback for score alone
        with patch.object(onewin_bridge.time, 'time', return_value=2_000_000_007):
            self.stream._on_match_info({'matchId': 123, 'service': None})
        self.assertIsNone(self.router.onewin_phase_evidence(self.slug)['ws_service'])

    def test_phase_status_update_dispatches_only_bound_markets(self):
        queue = DirtyUpdateQueue()
        self.router.set_adapter_update_callback(queue.put_adapter_event)
        self.stream._on_match_info({'matchId': 123, 'service': 'LIVE'})
        events = queue.drain(timeout=0)
        self.assertEqual(resolve_dirty_slugs(events, {}, self.router.adapter_event_index()), {self.slug})

    def test_reconnect_clears_old_status_before_new_pre_ack_message(self):
        self.stream._on_match_info({'matchId': 123, 'service': 'PREMATCH'})
        self.stream._connected.clear()
        incoming = '42' + json.dumps(['u', {'messageType': 'match-info',
                                             'data': {'matchId': 123, 'service': 'LIVE'}}])
        socket = Mock()
        socket.recv.side_effect = ['0{}', incoming, '40', '41']
        with patch.object(onewin_bridge.websocket, 'create_connection', return_value=socket), \
             patch.object(onewin_bridge.time, 'time', return_value=2_000_000_008):
            self.stream._connect_and_loop()
        self.assertFalse(self.stream._connected.is_set())
        self.assertEqual(self.stream.matches['123']['_phase_ws_service'], 'LIVE')
        self.assertEqual(self.stream.matches['123']['_phase_ws_service_at'], 2_000_000_008)

    def test_catalog_refresh_wakes_only_bound_ids_and_out_of_order_does_not_overwrite_phase(self):
        seen = []
        self.stream.set_odds_update_callback(seen.append)
        newer = dict(self.match, service='LIVE', serviceV2='live', _phase_catalog_received_at=2_000_000_020)
        older = dict(self.match, _phase_catalog_received_at=2_000_000_010)
        unbound = dict(self.match, id=456, _phase_catalog_received_at=2_000_000_021)
        self.stream._store_matches([newer, unbound])
        self.stream._store_matches([older])
        self.assertEqual(seen, ['123', '123'])
        evidence = self.stream.phase_evidence(self.slug, 123)
        self.assertEqual((evidence['service'], evidence['serviceV2'], evidence['catalog_at']),
                         ('LIVE', 'live', 2_000_000_020))

    def test_phase_and_source_survive_backend_and_frozen_snapshot(self):
        snapshot = {'slug': self.slug, 'q': 'Alpha vs Beta', 'sport': 'CS2', 'market_type': 'MATCH',
                    'phase': 'prematch', 'phase_source': '1win', 'phase_reason': 'onewin_prematch',
                    'phase_start_difference_sec': 300, 'roi': .06, 'stakes': [1, 1],
                    'bets': [{'source': '1win', 'outcome': 'Alpha'},
                             {'source': 'Polymarket', 'outcome': 'Beta', 'token': 't'}]}
        pinned = {}
        self.assertTrue(polyarb_v4._pin_signal_snapshot(pinned, snapshot, now=1000))
        snapshot['phase'] = 'unknown'
        snapshot['phase_source'] = 'unknown'
        frozen = next(iter(pinned.values()))
        self.assertEqual((frozen['phase'], frozen['phase_source']), ('prematch', '1win'))
        with patch.object(web_app, '_load_config', return_value={'safe_fork_threshold': .1}):
            enriched = web_app._build_enriched({self.slug: frozen})[self.slug]
        self.assertEqual((enriched['phase'], enriched['phase_source']), ('prematch', '1win'))
        self.assertTrue(enriched['inactive_snapshot'])

    def test_catalog_to_bound_stream_to_scanner_and_json_log(self):
        now = timestamp('2026-11-26T15:12:54Z')
        pm = FIXTURE['polymarket']
        event = dict(pm['event'])
        event['markets'] = [dict(pm['market'], slug=FIXTURE['market_slug'],
                                 question='Alpha vs Beta - Map 2',
                                 outcomes='["Alpha", "Beta"]', clobTokenIds='["a", "b"]')]
        response = Mock(status_code=200)
        response.json.return_value = {'events': [event], 'next_cursor': 'LTE='}
        response.raise_for_status.return_value = None
        with patch.object(pm_catalog.requests, 'get', return_value=response), \
             patch.object(pm_catalog.time, 'time', return_value=now.timestamp() - 2):
            markets, complete = pm_catalog.fetch_live_pm_markets(
                with_status=True, tags=['cs2'], prefixes=('cs2-',), worker_count=1)
        self.assertTrue(complete)
        slug = FIXTURE['market_slug']
        market = markets[slug]
        self.assertEqual(market['_pm_phase_evidence']['received_at'], now.timestamp() - 2)
        ow = FIXTURE['onewin']
        with patch.object(onewin_bridge.time, 'time', return_value=now.timestamp() - 1):
            self.stream._store_matches([dict(self.match, id=ow['match_id'], startAt=ow['startAt'])])
        self.stream.bindings[slug] = {'match_id': ow['match_id']}
        self.router.bindings[slug] = ('1win', {'match_id': ow['match_id']})
        result = polyarb_v4._resolved_market_phase(market, self.router, slug, now=now)
        self.assertEqual((result['phase'], result['source']), ('prematch', '1win'))
        phase_market = dict(market, phase=result['phase'], _phase_classified=True,
                            phase_source=result['source'], phase_reason=result['reason'],
                            phase_start_difference_sec=result.get('start_difference_sec'))
        output = io.StringIO()
        class NoClose(io.StringIO):
            def close(self):
                pass
        output = NoClose()
        with patch('builtins.open', return_value=output):
            polyarb_v4.log_arb_json(slug, 'CS2', .06, 100, 6, 106,
                                   [{'source': '1win', 'outcome': 'Alpha', 'o': 2}],
                                   [10], phase_market, 1)
        row = json.loads(output.getvalue())
        self.assertEqual((row['phase'], row['phase_source'], row['phase_start_difference_sec']),
                         ('prematch', '1win', 300.0))

    def test_phase_label_node_vm_without_browser(self):
        node = shutil.which('node')
        if not node:
            try:
                from playwright._impl._driver import compute_driver_executable
                node = compute_driver_executable()[0]
            except ImportError:
                self.skipTest('Node.js unavailable')
        result = subprocess.run([node, str(ROOT / 'tests/dashboard_phase_node.js')],
                                cwd=ROOT, capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
