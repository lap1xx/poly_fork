#!/usr/bin/env python3
"""Track observed Polymarket best ask/bid snapshots for the local dashboard.

The history only accepts explicit bid/ask fields emitted by the scanner. Older
snapshots only contained an effective payout multiplier, which is not enough to
reconstruct both sides of the order book and is intentionally ignored.
"""

import json
import math
import os
import pathlib
import threading
import time
from collections import deque
from typing import Deque, Dict, Tuple

HERE = pathlib.Path(__file__).parent
LIVE_FILE = HERE / "arbs_live.json"
HISTORY_FILE = HERE / "pm_price_history.json"

MAX_POINTS = 240
POLL_SEC = 5.0
CLEANUP_AGE_SEC = 900.0
CHANGE_EPSILON = 0.0005

_lock = threading.RLock()
_lifecycle_lock = threading.Lock()
_history: Dict[str, Deque[Tuple[float, float, float]]] = {}
_stop = threading.Event()
_thread = None
_loaded = False


def _load():
    """Load persisted history once; ignore malformed points independently."""
    global _loaded
    with _lifecycle_lock:
        if _loaded:
            return
        _loaded = True
    if not HISTORY_FILE.exists():
        return
    try:
        raw = json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
    except Exception:
        return
    if not isinstance(raw, dict):
        return
    with _lock:
        for slug, points in raw.items():
            if not isinstance(slug, str) or not isinstance(points, list):
                continue
            dq: Deque[Tuple[float, float, float]] = deque(maxlen=MAX_POINTS)
            for point in points[-MAX_POINTS:]:
                try:
                    ts, ask, bid = (float(point[i]) for i in range(3))
                    if not all(math.isfinite(x) for x in (ts, ask, bid)):
                        continue
                    if not (0.0 < bid <= ask < 1.0):
                        continue
                    dq.append((ts, ask, bid))
                except (TypeError, ValueError, IndexError):
                    continue
            if dq:
                _history[slug] = dq


def _save():
    with _lock:
        snapshot = {key: list(points) for key, points in _history.items()}
    tmp = HISTORY_FILE.with_name(HISTORY_FILE.name + ".tmp")
    try:
        tmp.write_text(json.dumps(snapshot, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, HISTORY_FILE)
    except Exception:
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass


def _extract_pm_price(arb: dict):
    """Return validated (best_ask, best_bid), or (None, None).

    Never derive bid and ask from an effective odds value: that value may
    include fees and average execution price, and cannot represent both sides.
    """
    if not isinstance(arb, dict):
        return None, None
    bets = arb.get("bets") or []
    if not isinstance(bets, list):
        return None, None
    for bet in bets:
        if not isinstance(bet, dict) or bet.get("source") != "Polymarket":
            continue
        ask = bet.get("pm_best_ask", arb.get("pm_best_ask"))
        bid = bet.get("pm_best_bid", arb.get("pm_best_bid"))
        try:
            ask, bid = float(ask), float(bid)
        except (TypeError, ValueError):
            return None, None
        if not (math.isfinite(ask) and math.isfinite(bid)):
            return None, None
        if not (0.0 < bid <= ask < 1.0):
            return None, None
        return ask, bid
    return None, None


def _poll_once():
    if not LIVE_FILE.exists():
        return
    try:
        raw = LIVE_FILE.read_text(encoding="utf-8")
        data = json.loads(raw) if raw else {}
    except Exception:
        return
    if not isinstance(data, dict):
        return
    now = time.time()
    with _lock:
        for key, arb in data.items():
            if not isinstance(arb, dict):
                continue
            slug = arb.get("slug") or key
            if not isinstance(slug, str) or not slug:
                continue
            ask, bid = _extract_pm_price(arb)
            if ask is None:
                continue
            points = _history.get(slug)
            if points is None:
                points = deque(maxlen=MAX_POINTS)
                _history[slug] = points
            if (not points
                    or abs(ask - points[-1][1]) > CHANGE_EPSILON
                    or abs(bid - points[-1][2]) > CHANGE_EPSILON):
                points.append((now, ask, bid))


def _loop():
    last_save = time.time()
    last_cleanup = time.time()
    try:
        while not _stop.is_set():
            try:
                _poll_once()
            except Exception:
                pass
            now = time.time()
            if now - last_save >= 30.0:
                _save()
                last_save = now
            if now - last_cleanup >= 120.0:
                cutoff = now - CLEANUP_AGE_SEC
                with _lock:
                    dead = [slug for slug, points in _history.items()
                            if not points or points[-1][0] < cutoff]
                    for slug in dead:
                        _history.pop(slug, None)
                last_cleanup = now
            _stop.wait(POLL_SEC)
    finally:
        _save()


def start():
    """Start the sampler; safe to call again after stop()."""
    global _thread
    with _lifecycle_lock:
        if _thread is not None and _thread.is_alive():
            return
        _stop.clear()
        if not _loaded:
            # _load uses the same lifecycle lock, so defer it until after release.
            pass
        should_load = not _loaded
    if should_load:
        _load()
    with _lifecycle_lock:
        if _thread is not None and _thread.is_alive():
            return
        _stop.clear()
        _thread = threading.Thread(target=_loop, name="pm-price-history", daemon=True)
        _thread.start()


def stop(timeout: float = 5.0):
    """Stop and join the sampler; an upcoming start() may restart it."""
    _stop.set()
    thread = _thread
    if thread is not None and thread.is_alive() and thread is not threading.current_thread():
        thread.join(timeout=max(0.0, float(timeout)))


def get_history(slug: str, max_points: int = 120):
    """Return validated {ts, ask, bid} points, oldest first."""
    try:
        limit = max(1, min(MAX_POINTS, int(max_points)))
    except (TypeError, ValueError):
        limit = 120
    with _lock:
        points = _history.get(slug)
        if not points:
            return []
        snapshot = list(points)[-limit:]
    now = time.time()
    return [
        {"ts": round(ts, 3), "ago": round(max(0.0, now - ts), 1),
         "ask": round(ask, 4), "bid": round(bid, 4)}
        for ts, ask, bid in snapshot
    ]
