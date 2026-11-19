"""Small, testable primitives for event-driven market recalculation."""
from __future__ import annotations

import queue
import threading
import time
from collections import defaultdict


def _key(value):
    return str(value).strip() if value is not None else ""


class DirtyUpdateQueue:
    """Thread-safe event queue that coalesces duplicate source updates."""

    def __init__(self):
        self._queue = queue.Queue()
        self._pending = set()
        self._lock = threading.Lock()

    def put(self, source, identifier):
        key = _key(identifier)
        if not key:
            return False
        event_key = (str(source), key)
        event = (event_key[0], event_key[1], time.time())
        with self._lock:
            if event_key in self._pending:
                return False
            self._pending.add(event_key)
            self._queue.put_nowait(event)
        return True

    def put_pm_token(self, token_id):
        return self.put("pm", token_id)

    def put_onewin_match(self, match_id):
        return self.put("1win", match_id)

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


def resolve_dirty_slugs(events, token_index, match_index):
    """Resolve source events to the exact PM markets that need recalculation."""
    dirty = set()
    for event in events or []:
        if len(event) < 2:
            continue
        source, identifier = event[:2]
        key = _key(identifier)
        if source == "pm":
            dirty.update(token_index.get(key, ()))
        elif source == "1win":
            dirty.update(match_index.get(key, ()))
    return dirty
