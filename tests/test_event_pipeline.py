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
from unittest.mock import patch

import bk_router
import polyarb_v4
import web_app
from event_dispatch import DirtyUpdateQueue, build_token_slug_index, resolve_dirty_slugs
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


class _FakeBrowserlessWS:
    def __init__(self, messages, host="127.0.0.1", headers=None):
        self.messages = list(messages)
        self.client = SimpleNamespace(host=host)
        self.headers = headers or {}
        self.accepted = False
        self.closed = None

    async def accept(self):
        self.accepted = True

    async def receive_text(self):
        if self.messages:
            return self.messages.pop(0)
        raise web_app.WebSocketDisconnect()

    async def close(self, code=1000):
        self.closed = code


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

    def _payload(self, seq=1, roi=0.07):
        return {
            "type": "scanner.snapshot",
            "schema_version": 1,
            "scanner_id": "polyarb_v4",
            "session_id": "test-session",
            "seq": seq,
            "sent_at": time.time(),
            "arbs": {"event-slug": {"roi": roi}},
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
            "arbs": {"event-slug": {"roi": 0.08}},
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
            live_file.write_text(json.dumps({"file-slug": {"roi": 0.09}}), encoding="utf-8")
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


if __name__ == "__main__":
    unittest.main()
