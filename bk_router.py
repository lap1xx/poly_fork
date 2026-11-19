# -*- coding: utf-8 -*-
"""
bk_router.py - фасад источников котировок.

Когда включён use_onewin, 1win является первичным источником; BetBoom остаётся
резервом для событий или рынков, которые 1win не смог сопоставить.

Интерфейс:
  router = get_router()
  binding, src = router.bind(slug, pm_market)
  bc = router.live_bc(slug)
  router.wait_bb(timeout=60)
  router.stats()
"""
from __future__ import annotations
import importlib
import threading
import time

try:
    import bb_bridge as _bb
    BB_OK = True
    _BB_ERR = None
except Exception as e:
    _bb = None
    BB_OK = False
    _BB_ERR = e

_ow = None
_OW_ERR = None


def _load_onewin():
    """Load the optional 1win adapter only after an explicit opt-in."""
    global _ow, _OW_ERR
    if _ow is not None:
        return _ow
    try:
        _ow = importlib.import_module("onewin_bridge")
        _OW_ERR = None
    except Exception as exc:
        _OW_ERR = exc
        return None
    return _ow


def is_prematch_slug(slug: str) -> bool:
    s = (slug or "").lower()
    for k in ("-game1", "-game2", "-game3", "-game4", "-game5",
              "-set-2", "-set-3", "-set-4", "-set-5", "-live", "-inplay"):
        if k in s:
            return False
    return True


class BKRouter:
    def __init__(self, use_bb=True, use_onewin=False):
        self.use_bb = use_bb and BB_OK
        # Keep module loading lazy; the scanner explicitly opts into 1win.
        self.use_onewin = bool(use_onewin and _load_onewin() is not None)
        self.bindings = {}
        self.fallback_bindings = {}
        self._markets = {}
        self._active_sources = {}
        self._bb_retry_after = {}
        self._onewin_retry_after = {}
        self._onewin_ready = None
        self._odds_update_callback = None
        self._onewin_stream = None
        self._lock = threading.RLock()

    def set_odds_update_callback(self, callback):
        """Set a lightweight listener for read-only 1win odds-cache updates."""
        self._odds_update_callback = callback
        if self._onewin_stream is not None:
            try:
                self._onewin_stream.set_odds_update_callback(callback)
            except Exception:
                pass

    def start(self):
        if self.use_bb:
            try:
                _bb.get_global_stream(verbose=False)
                print("[ROUTER] BB stream started")
            except Exception as e:
                print(f"[ROUTER] BB start failed: {e}")
                self.use_bb = False
        if self.use_onewin:
            try:
                stream = _ow.get_global_stream(verbose=False)
                self._onewin_stream = stream
                if self._odds_update_callback is not None:
                    stream.set_odds_update_callback(self._odds_update_callback)
                print("[ROUTER] 1win stream started")
            except Exception as e:
                print(f"[ROUTER] 1win start failed: {e}")
                self.use_onewin = False
                self._onewin_ready = False

    def stop(self):
        if self.use_bb:
            try:
                _bb.stop_global_stream()
            except Exception:
                pass
        if self.use_onewin:
            try:
                _ow.stop_global_stream()
            except Exception:
                pass

    def wait_bb(self, timeout=60):
        if not self.use_bb:
            return False
        try:
            return _bb.wait_ready(timeout=timeout)
        except Exception:
            return False

    def wait_onewin(self, timeout=60, min_matches=10):
        if not self.use_onewin:
            self._onewin_ready = False
            return False
        try:
            self._onewin_ready = bool(
                _ow.wait_ready(timeout=timeout, min_matches=min_matches)
            )
            return self._onewin_ready
        except Exception:
            self._onewin_ready = False
            return False

    def _can_use_onewin(self):
        if not self.use_onewin:
            return False
        if self._onewin_ready is True or self._onewin_ready is None:
            return True
        # Allow later binds to use the primary after a temporary outage.
        try:
            ready = bool(_ow.get_global_stream().is_ready())
        except Exception:
            ready = False
        if ready:
            self._onewin_ready = True
        return ready

    def _try_bind_bb_fallback(self, slug, pm_market=None, force=False):
        if not self.use_bb:
            return None
        now = time.monotonic()
        with self._lock:
            existing = self.fallback_bindings.get(slug)
            if existing:
                return existing
            if pm_market is not None:
                self._markets[slug] = pm_market
            market = self._markets.get(slug)
            last_try = self._bb_retry_after.get(slug, 0.0)
            if not force and now - last_try < 5.0:
                return None
            self._bb_retry_after[slug] = now
        if not market:
            return None
        try:
            binding = _bb.bind_slug(slug, market)
        except Exception:
            binding = None
        if binding:
            with self._lock:
                self.fallback_bindings[slug] = binding
        return binding

    def _try_promote_onewin(self, slug):
        if not self._can_use_onewin():
            return False
        now = time.monotonic()
        with self._lock:
            pair = self.bindings.get(slug)
            market = self._markets.get(slug)
            last_try = self._onewin_retry_after.get(slug, 0.0)
            if not pair or pair[0] != "BetBoom" or not market or now - last_try < 5.0:
                return False
            self._onewin_retry_after[slug] = now
        pm_teams = (market or {}).get("o") or []
        if len(pm_teams) != 2:
            return False
        try:
            binding = _ow.bind_slug(slug, pm_teams, market)
        except Exception:
            binding = None
        if not binding:
            return False
        with self._lock:
            pair = self.bindings.get(slug)
            if not pair or pair[0] != "BetBoom":
                return False
            self.fallback_bindings.setdefault(slug, pair[1])
            self.bindings[slug] = ("1win", binding)
            self._active_sources[slug] = "1win"
        return True

    def bind(self, slug, pm_market):
        pm_o = (pm_market or {}).get("o") or []
        if not pm_o or len(pm_o) != 2:
            return None, None
        with self._lock:
            self._markets[slug] = pm_market

        # Prefer the verified 1win feed for supported winner markets. Keep BB
        # bound as a live fallback for temporary/missing 1win quotes as well.
        if self._can_use_onewin():
            try:
                b = _ow.bind_slug(slug, pm_o, pm_market)
            except Exception:
                b = None
            if b:
                with self._lock:
                    self.bindings[slug] = ("1win", b)
                    self._active_sources[slug] = "1win"
                self._try_bind_bb_fallback(slug, pm_market, force=True)
                return b, "1win"

        if self.use_bb:
            try:
                b = _bb.bind_slug(slug, pm_market)
            except Exception:
                b = None
            if b:
                with self._lock:
                    self.bindings[slug] = ("BetBoom", b)
                    self._active_sources[slug] = "BetBoom"
                return b, "BetBoom"
        return None, None

    def live_bc(self, slug):
        with self._lock:
            pair = self.bindings.get(slug)
        if not pair:
            if self.use_bb:
                try:
                    bc = _bb.get_live_bc(slug)
                    if bc:
                        with self._lock:
                            self._active_sources[slug] = "BetBoom"
                        return bc
                except Exception:
                    pass
            return {}
        src, _ = pair
        if src == "BetBoom":
            if self._try_promote_onewin(slug):
                return self.live_bc(slug)
            try:
                bc = _bb.get_live_bc(slug) or {}
                if bc:
                    with self._lock:
                        self._active_sources[slug] = "BetBoom"
                return bc
            except Exception:
                return {}
        if src == "1win":
            try:
                bc = _ow.get_live_bc(slug) or {}
            except Exception:
                bc = {}
            if bc:
                with self._lock:
                    self._active_sources[slug] = "1win"
                return bc
            if self.use_bb:
                fallback = self._try_bind_bb_fallback(slug)
                if fallback:
                    try:
                        bc = _bb.get_live_bc(slug) or {}
                    except Exception:
                        bc = {}
                    if bc:
                        with self._lock:
                            self._active_sources[slug] = "BetBoom"
                        return bc
            return {}
        return {}

    def get_src(self, slug):
        with self._lock:
            pair = self.bindings.get(slug)
            return self._active_sources.get(slug) or (pair[0] if pair else None)

    def get_binding(self, slug):
        with self._lock:
            source = self._active_sources.get(slug)
            pair = self.bindings.get(slug)
            if source == "BetBoom" and slug in self.fallback_bindings:
                return self.fallback_bindings[slug]
            return pair[1] if pair else None

    def onewin_match_index(self):
        """Return canonical 1win match IDs mapped to every bound PM market."""
        index = {}
        with self._lock:
            pairs = list(self.bindings.items())
        for slug, pair in pairs:
            if not pair or pair[0] != "1win":
                continue
            binding = pair[1] or {}
            match_id = binding.get("match_id") if isinstance(binding, dict) else None
            if match_id is None:
                continue
            index.setdefault(str(match_id).strip(), set()).add(str(slug))
        return index

    def bb_stakes_debug(self, slug):
        if not self.use_bb:
            return []
        src = self.get_src(slug)
        if src != "BetBoom":
            return []
        try:
            from bb_bridge import get_binding as _gb, get_global_stream as _g2
            _bb_b = _gb(slug)
            if not _bb_b:
                return []
            _s2 = _g2()
            out = []
            for _st in _s2.snapshot_stakes(_bb_b["match_id"]):
                _mn = (_st.get("market_name") or "").lower()
                _pn = (_st.get("period_name") or "").lower()
                _want = (
                    "map" in _mn or "карт" in _mn
                    or "winner" in _mn or "исход" in _mn
                    or "map" in _pn or "карт" in _pn
                )
                if not _want:
                    continue
                out.append({
                    "market_name": _st.get("market_name"),
                    "period_name": _st.get("period_name"),
                    "market_slug": _st.get("market_slug"),
                    "display_name": _st.get("display_name"),
                    "short_name": _st.get("short_name"),
                    "name": _st.get("name"),
                    "factor": _st.get("factor"),
                    "map_num": _st.get("map_num"),
                    "is_active": _st.get("is_active"),
                    "url": (_st.get("url") or "")[:140],
                })
            return out
        except Exception as e:
            return [{"err": str(e)}]

    def stats(self, slugs=None):
        with self._lock:
            binding_items = (
                list(self.bindings.items()) if slugs is None
                else [(slug, self.bindings[slug]) for slug in slugs
                      if slug in self.bindings]
            )
        bindings = [pair for _, pair in binding_items]
        onewin_slugs = [slug for slug, pair in binding_items
                        if pair and pair[0] == "1win"]
        out = {
            "bb_matches": 0,
            "onewin_matches": 0,
            "onewin_fresh_pairs": 0,
            "bindings": len(bindings),
            "bound_1win": sum(1 for pair in bindings if pair and pair[0] == "1win"),
            "bound_betboom": sum(1 for pair in bindings if pair and pair[0] == "BetBoom"),
        }
        if self.use_bb:
            try:
                s = _bb.get_global_stream()
                with s.lock:
                    out["bb_matches"] = len(s.matches)
            except Exception:
                pass
        if self.use_onewin:
            try:
                s = _ow.get_global_stream()
                with s.lock:
                    out["onewin_matches"] = len(s.matches)
                live_bc = getattr(s, "live_bc", None)
                if callable(live_bc):
                    for slug in onewin_slugs:
                        try:
                            if live_bc(slug):
                                out["onewin_fresh_pairs"] += 1
                        except Exception:
                            continue
            except Exception:
                pass
        return out

    def feed_health(self):
        """Return read-only transport and quote freshness health for enabled feeds."""
        out = {}
        if self.use_onewin:
            try:
                out["1win"] = _ow.get_global_stream().snapshot_health()
            except Exception as exc:
                out["1win"] = {"connected": False, "error": type(exc).__name__}
        return out


_ROUTER = None
_ROUTER_LOCK = threading.Lock()


def get_router(use_bb=True, use_onewin=False):
    global _ROUTER
    with _ROUTER_LOCK:
        if _ROUTER is None:
            _ROUTER = BKRouter(use_bb=use_bb, use_onewin=use_onewin)
        return _ROUTER
