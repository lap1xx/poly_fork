#!/usr/bin/env python3
"""
price_history.py - track PM price changes for chart rendering.

Keeps a rolling buffer of (ts, best_ask, best_bid) per PM slug.
Runs a background thread that polls arbs_live.json every N seconds
and appends current PM price to the history of each slug.

Used by web_app.py /api/price_history/{slug}.
"""

import json
import pathlib
import threading
import time
from collections import deque
from typing import Deque, Dict, Tuple

HERE = pathlib.Path(__file__).parent
LIVE_FILE = HERE / "arbs_live.json"
HISTORY_FILE = HERE / "pm_price_history.json"

# buffer per slug: up to 240 points (20 min at 5s interval)
MAX_POINTS = 240
POLL_SEC = 5.0
CLEANUP_AGE_SEC = 900.0  # drop slugs not seen for 15 min

_lock = threading.Lock()
_history: Dict[str, Deque[Tuple[float, float, float]]] = {}
_stop = threading.Event()
_thread = None

def _load():
    global _history
    if not HISTORY_FILE.exists():
        return
    try:
        raw = json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
    except Exception:
        return
    with _lock:
        for slug, pts in raw.items():
            dq: Deque[Tuple[float, float, float]] = deque(maxlen=MAX_POINTS)
            for p in pts[-MAX_POINTS:]:
                try:
                    ts, ask, bid = float(p[0]), float(p[1]), float(p[2])
                    dq.append((ts, ask, bid))
                except Exception:
                    continue
            if dq:
                _history[slug] = dq

def _save():
    with _lock:
        snap = {k: list(v) for k, v in _history.items()}
    try:
        HISTORY_FILE.write_text(
            json.dumps(snap, ensure_ascii=False),
            encoding="utf-8",
        )
    except Exception:
        pass

def _extract_pm_price(arb: dict):
    """Return (best_ask, best_bid) from a recorded arb entry, or (None, None)."""
    bets = arb.get("bets") or []
    for b in bets:
        if b.get("source") != "Polymarket":
            continue
        try:
            o = float(b.get("o") or 0)
        except Exception:
            continue
        if o > 1.0:
            return 1.0 / o, 1.0 / o
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
        for slug, arb in data.items():
            if not isinstance(arb, dict):
                continue
            ask, bid = _extract_pm_price(arb)
            if ask is None:
                continue
            dq = _history.get(slug)
            if dq is None:
                dq = deque(maxlen=MAX_POINTS)
                _history[slug] = dq
            # не писать точку, если цена не изменилась (убирает шум)
            last_ask = dq[-1][1] if dq else None
            if last_ask is None or abs(float(ask) - last_ask) > 0.0005:
                dq.append((now, float(ask), float(bid or ask)))

def _loop():
    _load()
    last_save = time.time()
    last_cleanup = time.time()
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
                dead = []
                for slug, dq in _history.items():
                    if not dq:
                        dead.append(slug)
                        continue
                    if dq[-1][0] < cutoff:
                        dead.append(slug)
                for slug in dead:
                    _history.pop(slug, None)
            last_cleanup = now
        _stop.wait(POLL_SEC)

def start():
    global _thread
    if _thread is not None and _thread.is_alive():
        return
    _thread = threading.Thread(target=_loop, daemon=True)
    _thread.start()

def stop():
    _stop.set()

def get_history(slug: str, max_points: int = 120):
    """Return list of {ts, ask, bid} for the slug, oldest first."""
    with _lock:
        dq = _history.get(slug)
        if not dq:
            return []
        pts = list(dq)[-max_points:]
    now = time.time()
    return [
        {
            "ts": round(ts, 3),
            "ago": round(now - ts, 1),
            "ask": round(ask, 4),
            "bid": round(bid, 4),
        }
        for ts, ask, bid in pts
    ]
