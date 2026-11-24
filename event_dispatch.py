"""Small, testable primitives for event-driven market recalculation."""
from __future__ import annotations

import queue
import threading
import time
from collections import defaultdict


def _key(value):
    return str(value).strip() if value is not None else ""


class DirtyUpdateQueue:
    """Thread-safe event queue that coalesces duplicate source updates.

    Events retain the compact legacy shape ``(source_id, event_id, received_at_epoch)``.
    ``source_id`` is ``pm`` for CLOB tokens and the adapter ID for provider events.
    """

    def __init__(self):
        self._queue = queue.Queue()
        self._pending = set()
        self._lock = threading.Lock()

    def put(self, source, identifier, received_at=None):
        source_key = _key(source)
        key = _key(identifier)
        if not source_key or not key:
            return False
        event_key = (source_key, key)
        event = (source_key, key, time.time() if received_at is None else received_at)
        with self._lock:
            if event_key in self._pending:
                return False
            self._pending.add(event_key)
            self._queue.put_nowait(event)
        return True

    def put_pm_token(self, token_id):
        return self.put("pm", token_id)

    def put_adapter_event(self, adapter_id, provider_event_id, received_at=None):
        """Queue a monotonic provider receipt as epoch time for legacy diagnostics."""
        if _key(adapter_id).lower() == "pm":
            return False
        received_at_epoch = time.time()
        if received_at is not None:
            try:
                age = time.monotonic() - float(received_at)
                if age >= 0:
                    received_at_epoch -= age
            except (TypeError, ValueError, OverflowError):
                pass
        return self.put(adapter_id, provider_event_id, received_at_epoch)

    def put_onewin_match(self, match_id):
        """Backward-compatible 1win callback target."""
        return self.put_adapter_event("1win", match_id)

    def drain(self, timeout=0.0, limit=10000):
        """Take up to ``limit`` unique updates, waiting only for the first one."""
        try:
            first = self._queue.get(timeout=max(0.0, float(timeout)))
        except queue.Empty:
            return []
        events = [first]
        first_key = (first[0], first[1])
        seen = {first_key}
        self._release_pending(first_key)
        while len(events) < limit:
            try:
                event = self._queue.get_nowait()
            except queue.Empty:
                break
            event_key = (event[0], event[1])
            self._release_pending(event_key)
            if event_key not in seen:
                seen.add(event_key)
                events.append(event)
        return events

    def _release_pending(self, event):
        with self._lock:
            self._pending.discard(event)

    def __len__(self):
        return self._queue.qsize()


def build_token_slug_index(market_map):
    """Map each Polymarket token ID to all PM markets that depend on it."""
    index = defaultdict(set)
    for slug, market in (market_map or {}).items():
        for token in (market or {}).get("t") or []:
            key = _key(token)
            if key:
                index[key].add(str(slug))
    return dict(index)


def build_adapter_event_index(bindings_by_adapter):
    """Build ``(adapter_id, provider_event_id) -> PM market keys`` index."""
    index = defaultdict(set)
    for adapter_id, event_to_slugs in (bindings_by_adapter or {}).items():
        adapter_key = _key(adapter_id)
        if not adapter_key or adapter_key.lower() == "pm":
            continue
        for event_id, slugs in (event_to_slugs or {}).items():
            event_key = _key(event_id)
            if event_key:
                index[(adapter_key, event_key)].update(
                    str(slug) for slug in (slugs or ()) if _key(slug)
                )
    return dict(index)


def resolve_dirty_slugs(events, token_index, adapter_event_index):
    """Resolve PM token or adapter event updates to dependent PM markets.

    Plain event-ID keys are also accepted for legacy 1win indexes.
    """
    dirty = set()
    for event in events or ():
        if not isinstance(event, (tuple, list)) or len(event) < 2:
            continue
        source, identifier = event[:2]
        key = _key(identifier)
        if not key:
            continue
        if source == "pm":
            dirty.update(token_index.get(key, ()))
        else:
            dirty.update(adapter_event_index.get((str(source), key), ()))
            dirty.update(adapter_event_index.get(key, ()))
    return dirty
