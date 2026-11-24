import asyncio
import importlib
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import bk_router
import polyarb_v4
import web_app
from event_dispatch import (
    DirtyUpdateQueue,
    build_adapter_event_index,
    build_token_slug_index,
    resolve_dirty_slugs,
)
from scanner_publisher import ScannerPublisher


class DirtyUpdateQueueTests(unittest.TestCase):
    def test_updates_coalesce_and_resolve_only_dependent_pm_slugs(self):
        queue = DirtyUpdateQueue()
        self.assertTrue(queue.put_pm_token("token-a"))
        self.assertFalse(queue.put_pm_token("token-a"))
        self.assertTrue(queue.put_onewin_match(501))
        events = queue.drain(timeout=0)
        self.assertEqual(len(events), 2)
        self.assertTrue(all(len(event) == 3 for event in events))
        self.assertEqual(len(queue), 0)

        token_index = build_token_slug_index({
            "market-a": {"t": ["token-a", "token-b"]},
            "market-b": {"t": ["token-a"]},
        })
        match_index = {"501": {"market-b", "market-c"}}
        self.assertEqual(
            resolve_dirty_slugs(events, token_index, match_index),
            {"market-a", "market-b", "market-c"},
        )

    def test_same_key_can_be_enqueued_again_after_dispatch(self):
        queue = DirtyUpdateQueue()
        self.assertTrue(queue.put_pm_token("token-a"))
        first = queue.drain(timeout=0)
        self.assertTrue(queue.put_pm_token("token-a"))
        second = queue.drain(timeout=0)
        self.assertEqual(first[0][:2], second[0][:2])
        self.assertGreaterEqual(second[0][2], first[0][2])

    def test_adapter_events_are_coalesced_and_isolated_by_adapter_id(self):
        queue = DirtyUpdateQueue()
        monotonic_now = time.monotonic()
        wall_now = time.time()
        self.assertTrue(queue.put_adapter_event("1win", 501, monotonic_now - 0.25))
        self.assertFalse(queue.put_adapter_event("1win", "501", monotonic_now - 0.1))
        self.assertTrue(queue.put_adapter_event("synthetic", 501, monotonic_now - 0.2))
        events = queue.drain(timeout=0)
        self.assertEqual([event[:2] for event in events],
                         [("1win", "501"), ("synthetic", "501")])
        self.assertAlmostEqual(events[0][2], wall_now - 0.25, delta=0.1)
        self.assertAlmostEqual(events[1][2], wall_now - 0.2, delta=0.1)

        index = build_adapter_event_index({
            "1win": {501: {"market-a", "market-b"}},
            "synthetic": {501: {"market-c"}},
            "pm": {"501": {"must-not-index"}},
        })
        self.assertEqual(
            resolve_dirty_slugs(events, {}, index),
            {"market-a", "market-b", "market-c"},
        )
        self.assertEqual(resolve_dirty_slugs([("unknown", "501", 1)], {}, index), set())


class FeedCallbackTests(unittest.TestCase):
    def test_pm_book_callback_runs_after_book_is_committed_and_lock_released(self):
        token = "event-pipeline-test-token"
        observed = []

        def callback(updated_token):
            acquired = polyarb_v4.LOCK.acquire(blocking=False)
            observed.append((updated_token, token in polyarb_v4.BOOKS, acquired))
            if acquired:
                polyarb_v4.LOCK.release()

        feed = polyarb_v4.Feed([])
        previous = polyarb_v4.BOOKS.get(token)
        try:
            with patch.object(polyarb_v4, "_PM_UPDATE_CALLBACK", callback):
                feed._a({
                    "event_type": "book",
                    "asset_id": token,
                    "bids": [{"price": "0.40", "size": "10"}],
                    "asks": [{"price": "0.41", "size": "12"}],
                })
            self.assertEqual(observed, [(token, True, True)])
        finally:
            with polyarb_v4.LOCK:
                if previous is None:
                    polyarb_v4.BOOKS.pop(token, None)
                else:
                    polyarb_v4.BOOKS[token] = previous

    def test_pm_last_trade_does_not_rejuvenate_orderbook_or_dispatch_update(self):
        token = "event-pipeline-last-trade-token"
        now = time.time()
        book_ts = now - polyarb_v4.BOOK_TTL - 2.0
        previous = polyarb_v4.BOOKS.get(token)
        polyarb_v4.BOOKS[token] = {
            "bids": [(0.40, 10.0)],
            "asks": [(0.41, 12.0)],
            "ts": book_ts,
            "last_change": book_ts,
        }
        updates = []
        try:
            with patch.object(polyarb_v4, "_PM_UPDATE_CALLBACK", updates.append):
                polyarb_v4.Feed([])._a({
                    "event_type": "last_trade_price",
                    "asset_id": token,
                    "price": "0.41",
                    "size": "25",
                    "side": "BUY",
                    "timestamp": str(int(now * 1000)),
                })
            with polyarb_v4.LOCK:
                self.assertEqual(polyarb_v4.BOOKS[token]["ts"], book_ts)
            self.assertEqual(polyarb_v4.best_ask(token), 0.0)
            self.assertEqual(updates, [])
        finally:
            with polyarb_v4.LOCK:
                if previous is None:
                    polyarb_v4.BOOKS.pop(token, None)
                else:
                    polyarb_v4.BOOKS[token] = previous

    def test_onewin_callback_runs_after_odds_message_is_applied(self):
        module = importlib.import_module("onewin_bridge")
        try:
            stream = module.OneWinStream()
            seen = []
            stream.set_odds_update_callback(seen.append)
            stream._on_match_odds({
                "matchId": 501,
                "oddsGroups": [{
                    "id": "winner",
                    "name": "Match winner",
                    "oddsList": [{"id": "odd-1", "name": "Home", "cf": 1.85}],
                }],
            }, snapshot=True)
            self.assertEqual(seen, [501])
            self.assertIn("501", stream.stakes)
        finally:
            sys.modules.pop("onewin_bridge", None)

    def test_router_index_excludes_non_onewin_bindings(self):
        router = bk_router.BKRouter(use_bb=False, use_onewin=False)
        router.bindings.update({
            "pm-one": ("1win", {"match_id": 501}),
            "pm-two": ("BetBoom", {"match_id": 502}),
            "pm-three": ("1win", {"match_id": "503"}),
        })
        self.assertEqual(
            router.onewin_match_index(),
            {"501": {"pm-one"}, "503": {"pm-three"}},
        )
        current = router.stats(["pm-one", "pm-two"])
        self.assertEqual(current["bindings"], 2)
        self.assertEqual(current["bound_1win"], 1)
        self.assertEqual(current["bound_betboom"], 1)

    def test_router_generic_event_index_keys_provider_and_event(self):
        router = bk_router.BKRouter(use_bb=False, use_onewin=False)
        router.bindings.update({
            "pm-one": ("1win", {"match_id": 501}),
            "pm-two": ("1win", {"match_id": "501"}),
            "pm-three": ("BetBoom", {"match_id": 501}),
            "pm-four": ("1win", {"provider_event_id": 502}),
        })
        self.assertEqual(router.adapter_event_index(), {
            ("1win", "501"): {"pm-one", "pm-two"},
            ("BetBoom", "501"): {"pm-three"},
        })

    def test_router_installs_normalized_callback_without_changing_onewin_facade(self):
        router = bk_router.BKRouter(use_bb=False, use_onewin=False)
        stream = SimpleNamespace(set_odds_update_callback=Mock())
        router._onewin_stream = stream
        received = []
        router.set_adapter_update_callback(lambda *args: received.append(args))
        callback = stream.set_odds_update_callback.call_args.args[0]
        callback(501)
        self.assertEqual(len(received), 1)
        self.assertEqual(received[0][:2], ("1win", "501"))
        self.assertIsInstance(received[0][2], float)


class _FakeBrowserlessWS:
    def __init__(self, messages, host="127.0.0.1", headers=None):
        self.messages = list(messages)
        self.client = SimpleNamespace(host=host)
        self.headers = headers or {}
        self.accepted = False
        self.closed = None
        self.sent_text = []

    async def accept(self):
        self.accepted = True

    async def receive_text(self):
        if self.messages:
            return self.messages.pop(0)
        raise web_app.WebSocketDisconnect()

    async def close(self, code=1000):
        self.closed = code

    async def send_text(self, message):
        self.sent_text.append(message)


class ScannerIngressTests(unittest.TestCase):
    def setUp(self):
        self.old_state = web_app._scanner_state
        self.old_session = web_app._scanner_session_id
        self.old_seq = web_app._scanner_seq
        self.old_wakeup = web_app._broadcast_wakeup
        self.old_clients = set(web_app._clients)
        web_app._scanner_state = None
        web_app._scanner_session_id = None
        web_app._scanner_seq = -1
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        web_app._broadcast_wakeup = asyncio.Event()

    def tearDown(self):
        web_app._scanner_state = self.old_state
        web_app._scanner_session_id = self.old_session
        web_app._scanner_seq = self.old_seq
        web_app._broadcast_wakeup = self.old_wakeup
        web_app._clients.clear()
        web_app._clients.update(self.old_clients)
        self.loop.close()
        asyncio.set_event_loop(None)

    @staticmethod
    def _valid_arb(slug, roi):
        return {
            "slug": slug,
            "bk_source": "1win",
            "roi": roi,
            "bets": [
                {"source": "Polymarket", "outcome": "Alpha", "token": "pm-alpha", "o": 2.1},
                {"source": "1win", "outcome": "Bravo", "o": 2.2},
            ],
        }

    def _payload(self, seq=1, roi=0.07):
        return {
            "type": "scanner.snapshot",
            "schema_version": 1,
            "scanner_id": "polyarb_v4",
            "session_id": "test-session",
            "seq": seq,
            "sent_at": time.time(),
            "arbs": {"event-slug": self._valid_arb("event-slug", roi)},
            "scan_status": {"state": "running", "ts": time.time()},
        }

    def test_local_authenticated_snapshot_updates_memory_and_wakeup(self):
        message = json.dumps(self._payload())
        ws = _FakeBrowserlessWS(
            [message], headers={"authorization": "Bearer unit-test-secret"}
        )
        with patch.dict(os.environ, {"POLYARB_SCANNER_WS_TOKEN": "unit-test-secret"}):
            self.loop.run_until_complete(web_app.scanner_ingest_ws(ws))
        self.assertTrue(ws.accepted)
        self.assertEqual(web_app._scanner_state["seq"], 1)
        self.assertIn("scanner_to_backend_ms", web_app._scanner_state["scan_status"])
        self.assertTrue(web_app._broadcast_wakeup.is_set())

    def test_ingress_drops_bookmaker_vs_bookmaker_candidates(self):
        payload = self._payload()
        payload["arbs"]["event-slug"]["bets"] = [
            {"source": "1win", "outcome": "Alpha", "o": 2.1},
            {"source": "BetBoom", "outcome": "Bravo", "o": 2.2},
        ]
        ws = _FakeBrowserlessWS(
            [json.dumps(payload)], headers={"authorization": "Bearer unit-test-secret"}
        )
        with patch.dict(os.environ, {"POLYARB_SCANNER_WS_TOKEN": "unit-test-secret"}):
            self.loop.run_until_complete(web_app.scanner_ingest_ws(ws))
        self.assertEqual(web_app._scanner_state["arbs"], {})
        self.assertEqual(
            web_app._scanner_state["scan_status"]["invalid_arb_pairs_dropped"], 1
        )

    def test_missing_auth_is_rejected_before_accept(self):
        ws = _FakeBrowserlessWS(["{}"], host="127.0.0.1", headers={})
        with patch.dict(os.environ, {"POLYARB_SCANNER_WS_TOKEN": "unit-test-secret"}):
            self.loop.run_until_complete(web_app.scanner_ingest_ws(ws))
        self.assertFalse(ws.accepted)
        self.assertEqual(ws.closed, 1008)

    def test_non_loopback_ingress_is_rejected(self):
        ws = _FakeBrowserlessWS(
            [json.dumps(self._payload())], host="203.0.113.7",
            headers={"authorization": "Bearer unit-test-secret"},
        )
        with patch.dict(os.environ, {"POLYARB_SCANNER_WS_TOKEN": "unit-test-secret"}):
            self.loop.run_until_complete(web_app.scanner_ingest_ws(ws))
        self.assertFalse(ws.accepted)
        self.assertEqual(ws.closed, 1008)

    def test_duplicate_sequence_does_not_replace_last_snapshot(self):
        first = self._payload(seq=1, roi=0.07)
        duplicate = self._payload(seq=1, roi=0.99)
        ws = _FakeBrowserlessWS(
            [json.dumps(first), json.dumps(duplicate)],
            headers={"authorization": "Bearer unit-test-secret"},
        )
        with patch.dict(os.environ, {"POLYARB_SCANNER_WS_TOKEN": "unit-test-secret"}):
            self.loop.run_until_complete(web_app.scanner_ingest_ws(ws))
        self.assertEqual(web_app._scanner_state["arbs"]["event-slug"]["roi"], 0.07)

    def test_direct_snapshot_is_fanned_out_without_reading_snapshot_file(self):
        class Client:
            def __init__(self):
                self.messages = []
                self.sent = asyncio.Event()

            async def send_text(self, message):
                self.messages.append(json.loads(message))
                self.sent.set()

        client = Client()
        web_app._clients.clear()
        web_app._clients.add(client)
        web_app._scanner_state = {
            "seq": 9,
            "received_at": time.time(),
            "arbs": {"event-slug": self._valid_arb("event-slug", 0.08)},
            "scan_status": {"state": "running", "ts": time.time()},
        }
        web_app._broadcast_wakeup.set()

        async def run_loop_once():
            task = asyncio.create_task(web_app._broadcast_loop())
            try:
                await asyncio.wait_for(client.sent.wait(), timeout=0.5)
            finally:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        with patch.object(
            web_app, "_build_enriched",
            side_effect=lambda raw: {slug: {"slug": slug, **arb} for slug, arb in raw.items()},
        ), \
             patch.object(web_app, "_load_config", return_value={}), \
             patch.object(web_app, "_load_json", side_effect=AssertionError("file fallback used")):
            self.loop.run_until_complete(run_loop_once())
        self.assertEqual(client.messages[0]["arbs"][0]["slug"], "event-slug")
        self.assertEqual(client.messages[0]["scan_status"]["delivery_transport"], "scanner_ws")

    def test_scanner_ingest_to_ui_snapshot_adds_opportunity_v1_and_keeps_legacy_arb(self):
        slug = "offline-e2e-market"
        sent_at = time.time()
        legacy_arb = {
            "slug": slug,
            "q": "Team A vs Team B",
            "sport": "cs2",
            "market_type": "winner",
            "phase": "prematch",
            "currency": "USDT",
            "roi": 0.04,
            "stakes": [20.0, 20.0],
            "profit": 1.6,
            "ts": sent_at,
            "bk_source": "1win",
            "bk_odds_age_sec": 2.0,
            "bets": [
                {
                    "source": "Polymarket", "outcome": "Team A",
                    "token": "offline-pm-token", "o": 1 / (0.4 + 0.05 * 0.4 * 0.6),
                    "pm_best_ask": 0.4, "pm_avg_ask": 0.4,
                    "pm_age": 1.0, "pm_book_age_sec": 1.0,
                    "pm_fee_enabled": True, "pm_fee_rate": 0.05,
                    "pm_fee_exponent": 1.0, "pm_fee_source": "clob_market_info",
                },
                {
                    "source": "1win", "outcome": "Team B", "o": 2.7,
                    "odds_age_sec": 2.0,
                },
            ],
        }
        payload = {
            "type": "scanner.snapshot", "schema_version": 1,
            "scanner_id": "polyarb_v4", "session_id": "offline-e2e",
            "seq": 1, "sent_at": sent_at, "arbs": {slug: legacy_arb},
            "scan_status": {"state": "running", "ts": sent_at},
        }
        ingress = _FakeBrowserlessWS(
            [json.dumps(payload)], headers={"authorization": "Bearer unit-test-secret"}
        )
        with patch.dict(os.environ, {"POLYARB_SCANNER_WS_TOKEN": "unit-test-secret"}):
            self.loop.run_until_complete(web_app.scanner_ingest_ws(ingress))
        self.assertEqual(web_app._scanner_state["sent_at"], sent_at)

        class Client:
            def __init__(self):
                self.messages = []
                self.sent = asyncio.Event()

            async def send_text(self, message):
                self.messages.append(json.loads(message))
                self.sent.set()

        client = Client()
        web_app._clients.clear()
        web_app._clients.add(client)
        web_app._broadcast_wakeup.set()

        async def run_loop_once():
            task = asyncio.create_task(web_app._broadcast_loop())
            try:
                await asyncio.wait_for(client.sent.wait(), timeout=0.5)
            finally:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        with patch.object(
            web_app, "_build_enriched",
            side_effect=lambda raw: {slug: {"slug": key, **arb} for key, arb in raw.items()},
        ), patch.object(web_app, "_load_config", return_value={}):
            self.loop.run_until_complete(run_loop_once())

        snapshot = client.messages[0]
        self.assertEqual(snapshot["type"], "snapshot")
        self.assertEqual(snapshot["arbs"][0]["bets"], legacy_arb["bets"])
        self.assertEqual(snapshot["arbs"][0]["roi"], 0.04)
        contract = snapshot["opportunities_v1"]
        self.assertEqual(contract["schema_version"], 1)
        self.assertEqual(contract["rejected"], [])
        self.assertEqual(len(contract["opportunities"]), 1)
        opportunity = contract["opportunities"][0]
        self.assertEqual(opportunity["market"]["slug"], slug)
        self.assertEqual(opportunity["status"], "active")
        self.assertEqual(opportunity["legs"][0]["price"], 0.4)
        self.assertEqual(opportunity["legs"][1]["price"], 2.7)

    def test_initial_ui_snapshot_contains_opportunity_v1_envelope(self):
        ws = _FakeBrowserlessWS(
            [], headers={"origin": "http://127.0.0.1:8080"}
        )
        with patch.object(web_app, "_build_enriched", return_value={}), \
             patch.object(web_app, "_load_config", return_value={}), \
             patch.object(web_app, "_load_json", side_effect=lambda _path, default: default):
            self.loop.run_until_complete(web_app.ws_endpoint(ws))
        self.assertTrue(ws.accepted)
        snapshot = json.loads(ws.sent_text[0])
        self.assertEqual(snapshot["type"], "snapshot")
        self.assertEqual(snapshot["arbs"], [])
        self.assertEqual(snapshot["opportunities_v1"]["schema_version"], 1)
        self.assertEqual(snapshot["opportunities_v1"]["opportunities"], [])

    def test_ui_opportunity_snapshot_marks_precise_1win_age_stale(self):
        sent_at = time.time()
        slug = "1win-ui-freshness-regression"
        legacy_arb = {
            "slug": slug,
            "q": "Alpha vs Bravo",
            "sport": "cs2",
            "market_type": "winner",
            "phase": "prematch",
            "currency": "USDT",
            "roi": 0.04,
            "stakes": [20.0, 20.0],
            "profit": 1.6,
            "ts": sent_at,
            "bk_source": "1win",
            "bk_odds_age_sec": 29.94,
            "bets": [
                {
                    "source": "Polymarket", "outcome": "Alpha",
                    "token": "ui-freshness-pm-token", "o": 1 / (0.4 + 0.05 * 0.4 * 0.6),
                    "pm_best_ask": 0.4, "pm_avg_ask": 0.4,
                    "pm_age": 1.0, "pm_book_age_sec": 1.0,
                    "pm_fee_enabled": True, "pm_fee_rate": 0.05,
                    "pm_fee_exponent": 1.0, "pm_fee_source": "clob_market_info",
                },
                {
                    "source": "1win", "outcome": "Bravo", "o": 2.7,
                    "odds_age_sec": 29.94,
                },
            ],
        }
        scanner_state = {
            "sent_at": sent_at, "session_id": "1win-ui-replay", "seq": 1,
        }
        with patch.object(web_app.time, "time", return_value=sent_at + 0.07):
            contract = web_app._opportunities_v1_snapshot(
                {slug: legacy_arb}, scanner_state
            )

        self.assertEqual(contract["rejected"], [])
        opportunity = contract["opportunities"][0]
        self.assertEqual(opportunity["status"], "stale")
        self.assertAlmostEqual(opportunity["legs"][1]["age_ms"], 30_010.0, places=3)
        self.assertAlmostEqual(opportunity["legs"][0]["age_ms"], 1_070.0, places=3)

    def test_stale_scanner_state_falls_back_to_latest_atomic_files(self):
        class Client:
            def __init__(self):
                self.messages = []
                self.sent = asyncio.Event()

            async def send_text(self, message):
                self.messages.append(json.loads(message))
                self.sent.set()

        client = Client()
        web_app._clients.clear()
        web_app._clients.add(client)
        web_app._scanner_state = {
            "seq": 3,
            "received_at": time.time() - web_app.SCANNER_STATE_STALE_SECONDS - 1,
            "arbs": {"old-slug": {"roi": 0.04}},
            "scan_status": {"state": "running", "ts": time.time() - 5},
        }
        web_app._broadcast_wakeup.set()

        async def run_loop_once():
            task = asyncio.create_task(web_app._broadcast_loop())
            try:
                await asyncio.wait_for(client.sent.wait(), timeout=0.5)
            finally:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        with tempfile.TemporaryDirectory() as tmp:
            live_file = Path(tmp) / "arbs_live.json"
            status_file = Path(tmp) / "scanner_status.json"
            live_file.write_text(json.dumps({
                "file-slug": self._valid_arb("file-slug", 0.09)
            }), encoding="utf-8")
            status_file.write_text(json.dumps({"state": "running", "ts": time.time()}), encoding="utf-8")
            with patch.object(web_app, "LIVE_FILE", live_file), \
                 patch.object(web_app, "STATUS_FILE", status_file), \
                 patch.object(
                     web_app, "_build_enriched",
                     side_effect=lambda raw: {slug: {"slug": slug, **arb} for slug, arb in raw.items()},
                 ), \
                 patch.object(web_app, "_load_config", return_value={}):
                self.loop.run_until_complete(run_loop_once())

        self.assertEqual(client.messages[0]["arbs"][0]["slug"], "file-slug")
        self.assertEqual(client.messages[0]["scan_status"]["delivery_transport"], "file_fallback")
        self.assertNotIn("scanner_to_backend_ms", client.messages[0]["scan_status"])


class ScannerPublisherTests(unittest.TestCase):
    class Socket:
        def __init__(self):
            self.sent = []
            self.lock = __import__("threading").Lock()

        def send(self, text):
            with self.lock:
                self.sent.append(text)

        def close(self):
            pass

    class WebsocketModule:
        def __init__(self):
            self.socket = ScannerPublisherTests.Socket()
            self.connected = []

        def create_connection(self, url, **kwargs):
            self.connected.append((url, kwargs))
            return self.socket

    def test_publisher_sends_versioned_snapshot_with_bearer_header(self):
        module = self.WebsocketModule()
        publisher = ScannerPublisher(
            "ws://127.0.0.1:8080/ws/scanner", "ephemeral-secret",
            websocket_module=module,
        )
        try:
            publisher.publish({"slug": {"roi": 0.1}}, {"cycle": 4})
            deadline = time.time() + 2
            payload = None
            while time.time() < deadline:
                with module.socket.lock:
                    messages = list(module.socket.sent)
                for message in reversed(messages):
                    candidate = json.loads(message)
                    if "slug" in candidate.get("arbs", {}):
                        payload = candidate
                        break
                if payload is not None:
                    break
                time.sleep(0.01)
            self.assertIsNotNone(payload)
            self.assertEqual(payload["type"], "scanner.snapshot")
            self.assertGreaterEqual(payload["seq"], 1)
            self.assertEqual(payload["arbs"]["slug"]["roi"], 0.1)
            self.assertEqual(module.connected[0][0], "ws://127.0.0.1:8080/ws/scanner")
            self.assertIn("Authorization: Bearer ephemeral-secret", module.connected[0][1]["header"])
        finally:
            publisher.close()

    def test_publisher_replays_latest_snapshot_after_socket_drop(self):
        class DropOnceSocket(self.Socket):
            def send(self, text):
                raise OSError("simulated disconnect")

        class ReconnectingModule:
            def __init__(self):
                self.calls = 0
                self.good_socket = ScannerPublisherTests.Socket()

            def create_connection(self, url, **kwargs):
                self.calls += 1
                if self.calls == 1:
                    return DropOnceSocket()
                return self.good_socket

        module = ReconnectingModule()
        publisher = ScannerPublisher(
            "ws://127.0.0.1:8080/ws/scanner", "ephemeral-secret",
            websocket_module=module, reconnect_seconds=0.01,
        )
        try:
            publisher.publish({"slug": {"roi": 0.2}}, {"cycle": 8})
            deadline = time.time() + 2
            payload = None
            while time.time() < deadline:
                with module.good_socket.lock:
                    messages = list(module.good_socket.sent)
                if messages:
                    payload = json.loads(messages[-1])
                    break
                time.sleep(0.01)
            self.assertGreaterEqual(module.calls, 2)
            self.assertIsNotNone(payload)
            self.assertEqual(payload["arbs"]["slug"]["roi"], 0.2)
        finally:
            publisher.close()


class SignalDropDiagnosticsTests(unittest.TestCase):
    def test_reason_classification_distinguishes_quote_loss_roi_and_market_removal(self):
        self.assertEqual(
            polyarb_v4._classify_signal_drop_reason(
                market_missing=True, has_fresh_bookmaker=False,
            ),
            "market_removed",
        )
        self.assertEqual(
            polyarb_v4._classify_signal_drop_reason(
                has_fresh_bookmaker=False, best_roi=0.1, min_roi=0.03,
            ),
            "no_fresh_bookmaker_pair",
        )
        self.assertEqual(
            polyarb_v4._classify_signal_drop_reason(
                has_fresh_bookmaker=True, best_roi=0.009, min_roi=0.03,
            ),
            "below_roi_threshold",
        )
        self.assertEqual(
            polyarb_v4._classify_signal_drop_reason(
                has_fresh_bookmaker=True,
                candidate_diagnostics={"pm_no_ask": 1},
            ),
            "pm_ask_unavailable",
        )
        self.assertEqual(
            polyarb_v4._classify_signal_drop_reason(
                has_fresh_bookmaker=True,
                candidate_diagnostics={"pm_no_ask": 1},
                pm_diagnostic={"book_state": "stale_book"},
            ),
            "pm_book_stale",
        )
        self.assertEqual(
            polyarb_v4._classify_signal_drop_reason(
                has_fresh_bookmaker=True,
                pm_diagnostic={"book_state": "missing_book"},
            ),
            "pm_book_missing",
        )

    def test_drop_record_keeps_last_executable_quotes_and_ages(self):
        previous = {
            "slug": "wta-example-event",
            "q": "Player A vs Player B",
            "sport": "TENNIS",
            "market_type": "MATCH",
            "roi": 0.009,
            "ts": 8.0,
            "bets": [
                {
                    "source": "Polymarket", "outcome": "Player A", "o": 4.18,
                    "pm_best_ask": 0.23, "pm_avg_ask": 0.24,
                    "pm_best_bid": 0.22, "pm_book_age_sec": 1.4,
                    "pm_target_usd": 20.0, "pm_target_shares": 86.96,
                },
                {
                    "source": "1win", "outcome": "Player B", "o": 1.33,
                    "odds_age_sec": 0.6,
                },
            ],
        }
        dropped = polyarb_v4._make_signal_drop_diagnostic(
            previous, "below_roi_threshold", 10.0,
            min_roi=0.03, current_best_roi=-0.002,
            current_pm_diagnostic={
                "book_state": "stale_book", "book_age_sec": 31.2,
                "bid_levels": 3, "ask_levels": 0, "rest_refresh": "fetch_failed",
            },
        )
        self.assertEqual(dropped["reason"], "below_roi_threshold")
        self.assertEqual(dropped["signal_snapshot_age_sec"], 2.0)
        self.assertEqual(dropped["previous_roi_pct"], 0.9)
        self.assertEqual(dropped["current_best_roi_pct"], -0.2)
        self.assertEqual(dropped["min_roi_pct"], 3.0)
        self.assertEqual(dropped["pm_quote"]["best_ask"], 0.23)
        self.assertEqual(dropped["pm_quote"]["average_ask"], 0.24)
        self.assertEqual(dropped["pm_quote"]["book_age_sec"], 1.4)
        self.assertEqual(dropped["bookmaker_quote"]["odds"], 1.33)
        self.assertEqual(dropped["bookmaker_quote"]["age_sec"], 0.6)
        self.assertEqual(dropped["current_pm_book"]["state"], "stale_book")
        self.assertEqual(dropped["current_pm_book"]["age_sec"], 31.2)
        self.assertEqual(dropped["current_pm_book"]["ask_levels"], 0)
        self.assertNotIn("bets", dropped)

    def test_recent_drop_history_keeps_ten_entries_for_five_minutes(self):
        now = 1000.0
        drops = {
            f"slug-{i}": {"slug": f"slug-{i}", "dropped_at": now - i * 5}
            for i in range(12)
        }
        drops["expired"] = {"slug": "expired", "dropped_at": now - 301}

        rows = polyarb_v4._prune_recent_signal_drops(drops, now)

        self.assertEqual(len(rows), 10)
        self.assertEqual(rows[0]["slug"], "slug-0")
        self.assertNotIn("expired", drops)
        self.assertNotIn("slug-10", drops)
        self.assertNotIn("slug-11", drops)


if __name__ == "__main__":
    unittest.main()


class PinnedSignalSnapshotTests(unittest.TestCase):
    def test_snapshot_freezes_quotes_stakes_and_roi_until_five_minute_expiry(self):
        snapshot = {
            "slug": "example-match",
            "q": "Alpha vs Beta",
            "roi": 0.061,
            "bk_event_url": "https://f1w-7707.com/betting/match/esport/example-match",
            "pm_event_url": "https://polymarket.com/event/example-match",
            "stakes": [42.0, 58.0],
            "bets": [
                {"source": "1win", "outcome": "Alpha", "o": 2.2},
                {"source": "Polymarket", "outcome": "Beta", "o": 1.9,
                 "token": "token-x", "pm_best_ask": 0.51, "pm_avg_ask": 0.52,
                 "pm_target_shares": 42.75},
            ],
            "ts": 1000.0,
        }
        pinned = {}
        self.assertTrue(polyarb_v4._pin_signal_snapshot(pinned, snapshot, now=1000.0))
        key = polyarb_v4._pinned_signal_key(snapshot)
        frozen = pinned[key]
        self.assertEqual(frozen["snapshot_id"], "%s|%.6f" % (key, 1000.0))
        self.assertEqual(frozen["currency"], "USDT")
        self.assertTrue(frozen["inactive_snapshot"])
        self.assertEqual(frozen["bk_event_url"], snapshot["bk_event_url"])
        self.assertEqual(frozen["pm_event_url"], snapshot["pm_event_url"])
        self.assertEqual(frozen["bets"][1]["token"], "token-x")
        self.assertEqual(frozen["bets"][1]["pm_target_shares"], 42.75)
        self.assertAlmostEqual(frozen["expires_at"], 1300.0)

        changed = json.loads(json.dumps(snapshot))
        changed["roi"] = 0.12
        changed["bets"][0]["o"] = 3.1
        changed["bets"][1]["pm_best_ask"] = 0.43
        changed["stakes"] = [99.0, 1.0]
        self.assertFalse(polyarb_v4._pin_signal_snapshot(pinned, changed, now=1050.0))
        self.assertEqual(pinned[key]["roi"], 0.061)
        self.assertEqual(pinned[key]["bets"][0]["o"], 2.2)
        self.assertEqual(pinned[key]["bets"][1]["pm_best_ask"], 0.51)
        self.assertEqual(pinned[key]["stakes"], [42.0, 58.0])

        self.assertEqual(len(polyarb_v4._prune_pinned_signal_snapshots(pinned, 1299.9)), 1)
        self.assertEqual(polyarb_v4._prune_pinned_signal_snapshots(pinned, 1300.0), [])

    def test_snapshot_threshold_is_fixed_at_five_percent(self):
        snapshot = {
            "slug": "below-threshold", "roi": 0.0499, "stakes": [1, 1],
            "bets": [
                {"source": "1win", "outcome": "A"},
                {"source": "Polymarket", "outcome": "B", "token": "token-y"},
            ],
        }
        pinned = {}
        self.assertFalse(polyarb_v4._pin_signal_snapshot(pinned, snapshot, now=1000.0))
        self.assertEqual(pinned, {})

    def test_distinct_pm_tokens_do_not_dedupe_within_the_snapshot_ttl(self):
        base = {
            "slug": "same-event", "roi": 0.06, "stakes": [10.0, 10.0],
            "bets": [
                {"source": "1win", "outcome": "Alpha"},
                {"source": "Polymarket", "outcome": "Beta", "token": "token-a"},
            ],
        }
        another = json.loads(json.dumps(base))
        another["bets"][1]["token"] = "token-b"
        pinned = {}

        self.assertTrue(polyarb_v4._pin_signal_snapshot(pinned, base, now=1000.0))
        self.assertTrue(polyarb_v4._pin_signal_snapshot(pinned, another, now=1001.0))
        self.assertEqual(len(pinned), 2)

    def test_more_than_fifty_snapshots_keep_the_full_five_minute_ttl(self):
        pinned = {}
        for index in range(75):
            snapshot = {
                "slug": f"market-{index}", "roi": 0.06, "stakes": [1.0, 1.0],
                "bets": [
                    {"source": "1win", "outcome": "A"},
                    {"source": "Polymarket", "outcome": "B", "token": f"token-{index}"},
                ],
            }
            self.assertTrue(polyarb_v4._pin_signal_snapshot(pinned, snapshot, now=1000.0))
        self.assertEqual(len(polyarb_v4._prune_pinned_signal_snapshots(pinned, 1299.9)), 75)
        self.assertEqual(polyarb_v4._prune_pinned_signal_snapshots(pinned, 1300.0), [])


class OneWinEventOriginTests(unittest.TestCase):
    def test_configured_https_origin_is_normalized(self):
        with patch.dict(os.environ, {"ONEWIN_ORIGIN": "https://F1W-7707.com/"}):
            self.assertEqual(web_app._configured_onewin_event_origin(), "https://f1w-7707.com")

    def test_non_https_or_credentialed_origin_is_rejected(self):
        for value in ("http://f1w-7707.com", "https://user@f1w-7707.com", "https://evil<svg>"):
            with self.subTest(value=value), patch.dict(os.environ, {"ONEWIN_ORIGIN": value}):
                self.assertEqual(web_app._configured_onewin_event_origin(), "")


class UsdtNormalizationTests(unittest.TestCase):
    def test_legacy_rub_trade_amount_uses_recorded_rate(self):
        self.assertEqual(web_app._trade_amount_usdt(
            {"bank": 8500, "rate": 85}, "bank"), 100.0)

    def test_new_usdt_trade_amount_is_not_converted(self):
        self.assertEqual(web_app._trade_amount_usdt(
            {"bank": 100, "rate": 85, "currency": "USDT"}, "bank"), 100.0)

    def test_legacy_scanner_bank_is_migrated_to_usdt_once(self):
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.json"
            config_path.write_text(json.dumps({"bank": 8500, "rate": 85}), encoding="utf-8")
            with patch.object(polyarb_v4, "CFG_FILE", str(config_path)):
                migrated = polyarb_v4.load_cfg()
                persisted = json.loads(config_path.read_text(encoding="utf-8"))
        self.assertEqual(migrated["bank"], 100.0)
        self.assertEqual(migrated["currency"], "USDT")
        self.assertEqual(persisted["rate"], 1.0)
        self.assertEqual(persisted["currency"], "USDT")
