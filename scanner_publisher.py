"""Read-only, reconnecting WebSocket publisher for the local dashboard."""
from __future__ import annotations

import json
import threading
import time
import uuid

SCANNER_WS_MAX_BYTES = 8 * 1024 * 1024


def _serialize_snapshot_with_book_fallback(payload, limit_bytes):
    """Omit only optional UI books if they make an ingest message too large.

    The input is not mutated. If the legacy payload alone is still too large,
    its returned byte count exceeds the limit and the caller must not send it.
    """
    def encode(value):
        text = json.dumps(value, ensure_ascii=False, allow_nan=False)
        return text, len(text.encode("utf-8"))

    text, size = encode(payload)
    if size <= limit_bytes:
        return text, size, 0

    omitted = 0

    def without_book(row):
        nonlocal omitted
        if not isinstance(row, dict) or "pm_book_snapshot" not in row:
            return row
        omitted += 1
        return {key: value for key, value in row.items() if key != "pm_book_snapshot"}

    arbs = payload.get("arbs")
    status = payload.get("scan_status")
    if not isinstance(arbs, dict) or not isinstance(status, dict):
        return text, size, omitted
    compact_arbs = {slug: without_book(row) for slug, row in arbs.items()}
    compact_status = dict(status)
    pinned = compact_status.get("pinned_signal_snapshots")
    if isinstance(pinned, list):
        compact_status["pinned_signal_snapshots"] = [without_book(row) for row in pinned]
    if not omitted:
        return text, size, omitted
    compact_status["pm_book_payload_omitted"] = omitted
    compact_payload = dict(payload, arbs=compact_arbs, scan_status=compact_status)
    text, size = encode(compact_payload)
    return text, size, omitted


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
        self._last_oversize_warning = float('-inf')
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

    def _drain_server_frames(self, ws, reader_stop, disconnected):
        """Read control frames so websocket-client can answer server pings."""
        try:
            while not reader_stop.is_set():
                frame = ws.recv()
                if frame is None:
                    break
        except Exception:
            pass
        finally:
            if not reader_stop.is_set():
                disconnected.set()
                self._wake.set()

    def _run(self):
        while not self._stop.is_set():
            ws = None
            reader = None
            reader_stop = None
            payload_bytes = None
            payload_seq = self._seq
            try:
                ws = self._websocket.create_connection(
                    self.url,
                    timeout=3.0,
                    header=["Authorization: Bearer " + self.token],
                )
                # The connection timeout above protects the handshake. Keep the
                # receive thread open so websocket-client processes server pings.
                ws.settimeout(None)
                reader_stop = threading.Event()
                disconnected = threading.Event()
                reader = threading.Thread(
                    target=self._drain_server_frames,
                    args=(ws, reader_stop, disconnected),
                    name="scanner-dashboard-ws-reader",
                    daemon=True,
                )
                reader.start()
                self._connected.set()
                self._last_error = None
                if self._verbose:
                    print("[*] scanner dashboard WebSocket connected", flush=True)
                # Reconnects must replay the newest state even if no new quote
                # arrives between a dropped socket and the next connection.
                self._wake.set()
                while not self._stop.is_set():
                    if disconnected.is_set():
                        raise ConnectionError("scanner WebSocket peer disconnected")
                    if not self._wake.wait(1.0):
                        continue
                    self._wake.clear()
                    if disconnected.is_set():
                        raise ConnectionError("scanner WebSocket peer disconnected")
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
                    serialized, payload_bytes, omitted_books = _serialize_snapshot_with_book_fallback(
                        payload, SCANNER_WS_MAX_BYTES
                    )
                    if payload_bytes > SCANNER_WS_MAX_BYTES:
                        # Do not cause a reconnect loop. A stale backend scanner
                        # state will fall back to the existing disk snapshots.
                        now = time.monotonic()
                        if self._verbose and now - self._last_oversize_warning >= 30.0:
                            print("[!] scanner dashboard snapshot too large: bytes=%s limit=%s "
                                  "omitted_pm_books=%s; using file fallback"
                                  % (payload_bytes, SCANNER_WS_MAX_BYTES, omitted_books), flush=True)
                            self._last_oversize_warning = now
                        continue
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
                        "[!] scanner dashboard WebSocket: %s event_epoch=%.3f seq=%s payload_bytes=%s "
                        "close_code=%s close_reason=%s"
                        % (error_type, time.time(), payload_seq, payload_bytes,
                           close_code, close_reason),
                        flush=True,
                    )
                self._last_error = error_type
                if not self._stop.is_set():
                    self._stop.wait(self._reconnect_seconds)
            finally:
                self._connected.clear()
                if reader_stop is not None:
                    reader_stop.set()
                if ws is not None:
                    try:
                        ws.close()
                    except Exception:
                        pass
                if reader is not None and reader.is_alive():
                    reader.join(timeout=0.2)
