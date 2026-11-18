# -*- coding: utf-8 -*-
"""
bk_router.py - фасад для БК (BetBoom + 1win).

Логика (Q1 от 04.10.2026):
  * LIVE-слаг: сначала BB, если пусто - 1win
  * PREMATCH-слаг: ТОЛЬКО 1win (BB в прематче не отдаёт нормальные кэфы)

Интерфейс:
  router = get_router()
  binding, src = router.bind(slug, pm_market)
  bc = router.live_bc(slug)
  router.wait_bb(timeout=60)
  router.stats()
"""
from __future__ import annotations
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

try:
    import onewin_bridge as _ow
    OW_OK = True
    _OW_ERR = None
except Exception as e:
    _ow = None
    OW_OK = False
    _OW_ERR = e


def is_prematch_slug(slug: str) -> bool:
    s = (slug or "").lower()
    for k in ("-game1", "-game2", "-game3", "-game4", "-game5",
              "-set-2", "-set-3", "-set-4", "-set-5", "-live", "-inplay"):
        if k in s:
            return False
    return True


class BKRouter:
    def __init__(self, use_bb=True, use_onewin=True):
        self.use_bb = use_bb and BB_OK
        self.use_onewin = use_onewin and OW_OK
        self.bindings = {}
        self._lock = threading.RLock()

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
                _ow.get_global_stream(verbose=False)
                print("[ROUTER] 1win stream started")
            except Exception as e:
                print(f"[ROUTER] 1win start failed: {e}")
                self.use_onewin = False

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
            return False
        try:
            return _ow.wait_ready(timeout=timeout, min_matches=min_matches)
        except Exception:
            return False

    def bind(self, slug, pm_market):
        src_pref = "1win" if is_prematch_slug(slug) else "BetBoom"
        pm_o = (pm_market or {}).get("o") or []
        if not pm_o or len(pm_o) != 2:
            return None, None

        if src_pref == "BetBoom" and self.use_bb:
            try:
                b = _bb.bind_slug(slug, pm_market)
            except Exception:
                b = None
            if b:
                with self._lock:
                    self.bindings[slug] = ("BetBoom", b)
                return b, "BetBoom"
            if self.use_onewin:
                try:
                    b = _ow.bind_slug(slug, pm_o)
                except Exception:
                    b = None
                if b:
                    with self._lock:
                        self.bindings[slug] = ("1win", b)
                    return b, "1win"
            return None, None

        if self.use_onewin:
            try:
                b = _ow.bind_slug(slug, pm_o)
            except Exception:
                b = None
            if b:
                with self._lock:
                    self.bindings[slug] = ("1win", b)
                return b, "1win"
        return None, None

    def live_bc(self, slug):
        with self._lock:
            pair = self.bindings.get(slug)
        if not pair:
            if self.use_bb:
                try:
                    bc = _bb.get_live_bc(slug)
                    if bc:
                        return bc
                except Exception:
                    pass
            return {}
        src, _ = pair
        if src == "BetBoom":
            try:
                return _bb.get_live_bc(slug) or {}
            except Exception:
                return {}
        if src == "1win":
            try:
                return _ow.get_live_bc(slug) or {}
            except Exception:
                return {}
        return {}

    def get_src(self, slug):
        with self._lock:
            pair = self.bindings.get(slug)
        return pair[0] if pair else None

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

    def stats(self):
        out = {"bb_matches": 0, "onewin_matches": 0, "bindings": len(self.bindings)}
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
            except Exception:
                pass
        return out


_ROUTER = None
_ROUTER_LOCK = threading.Lock()


def get_router(use_bb=True, use_onewin=True):
    global _ROUTER
    with _ROUTER_LOCK:
        if _ROUTER is None:
            _ROUTER = BKRouter(use_bb=use_bb, use_onewin=use_onewin)
        return _ROUTER