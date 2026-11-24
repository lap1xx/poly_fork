"""Read-only, reconnecting WebSocket publisher for the local dashboard."""
from __future__ import annotations

import json
import threading
import time
import uuid


class ScannerPublisher:
    def __init__(self, url, token, websocket_module=None, reconnect_seconds=1.0, verbose=False):
        if websocket_module is None:
            import websocket as websocket_module
        self.url = str(url)
        self.token = str(token)
        self._websocket = websocket_module
        self._reconnect_seconds = max(0.1, float(reconnect_seconds))
        self._verbose = bool(verbose)
        self.session_id = uuid.uuid4().hex
        self._seq = 0
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._connected = threading.Event()
        self._last_error = None
        self._lock = threading.Lock()
        self._latest = {"arbs": {}, "scan_status": {}}
        self._thread = threading.Thread(
            target=self._run, name="scanner-dashboard-ws", daemon=True
        )
        self._thread.start()

    def publish(self, arbs, scan_status):
        """Replace pending state; bursts collapse to the newest consistent snapshot."""
        with self._lock:
            self._latest = {
                "arbs": dict(arbs or {}),
                "scan_status": dict(scan_status or {}),
            }
        self._wake.set()

    def close(self, timeout=1.0):
        self._stop.set()
        self._wake.set()
        if self._thread.is_alive():
            self._thread.join(timeout=max(0.0, float(timeout)))

    def _run(self):
        while not self._stop.is_set():
            ws = None
            payload_bytes = None
            payload_seq = self._seq
            try:
                ws = self._websocket.create_connection(
                    self.url,
                    timeout=3.0,
                    header=["Authorization: Bearer " + self.token],
                )
                self._connected.set()
                self._last_error = None
                if self._verbose:
                    print("[*] scanner dashboard WebSocket connected", flush=True)
                # Reconnects must replay the newest state even if no new quote
                # arrives between a dropped socket and the next connection.
                self._wake.set()
                while not self._stop.is_set():
                    if not self._wake.wait(1.0):
                        continue
                    self._wake.clear()
                    with self._lock:
                        state = self._latest
                        arbs = dict(state["arbs"])
                        status = dict(state["scan_status"])
                    self._seq += 1
                    payload_seq = self._seq
                    payload = {
                        "type": "scanner.snapshot",
                        "schema_version": 1,
                        "scanner_id": "polyarb_v4",
                        "session_id": self.session_id,
                        "seq": self._seq,
                        "sent_at": time.time(),
                        "arbs": arbs,
                        "scan_status": status,
                    }
                    serialized = json.dumps(payload, ensure_ascii=False, allow_nan=False)
                    payload_bytes = len(serialized.encode("utf-8"))
                    ws.send(serialized)
            except Exception as exc:
                was_connected = self._connected.is_set()
                self._connected.clear()
                error_type = type(exc).__name__
                if self._verbose and (was_connected or error_type != self._last_error):
                    close_code = getattr(ws, "close_status_code", None)
                    close_reason = getattr(ws, "close_reason", None)
                    if isinstance(close_reason, bytes):
                        close_reason = close_reason.decode("utf-8", errors="replace")
                    if close_reason is not None:
                        close_reason = str(close_reason)[:120]
                    print(
                        "[!] scanner dashboard WebSocket: %s seq=%s payload_bytes=%s "
                        "close_code=%s close_reason=%s"
                        % (error_type, payload_seq, payload_bytes, close_code, close_reason),
                        flush=True,
                    )
                self._last_error = error_type
                if not self._stop.is_set():
                    self._stop.wait(self._reconnect_seconds)
            finally:
                self._connected.clear()
                if ws is not None:
                    try:
                        ws.close()
                    except Exception:
                        pass
