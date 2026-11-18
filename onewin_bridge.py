# -*- coding: utf-8 -*-
"""
1win bridge (Socket.IO / Engine.IO v4) for Poly-ARB.

Module-level API (compatible with bb_bridge):
  get_global_stream(verbose=False)
  bind_slug(slug, pm_teams) -> binding | None
  get_live_bc(slug) -> {TeamName: {'src':'1win','o':cf,'c':0.0}}
  get_binding(slug) -> dict | None
  wait_ready(timeout, min_matches) -> bool
  stop_global_stream()

Smoke test:
  python -B onewin_bridge.py --verbose

v4 fix: ping-loop starts BEFORE fetch_live_matches (5s HTTP blocks WS otherwise).
"""

import json
import re
import sys
import time
import threading
import argparse

try:
    import websocket
except ImportError:
    print("[1WIN] pip install websocket-client")
    sys.exit(1)

try:
    import requests
except ImportError:
    print("[1WIN] pip install requests")
    sys.exit(1)

PARTNER_ID = "44ba10e5-7df2-47ab-a44d-dc93803c7a6e"
MIRROR = "one-vv0702.com"
HTTP_BASE = "https://api-gateway.top-parser.com"
HTTP_MATCHES = HTTP_BASE + "/matches/get-many"

WS_URL = (
    "wss://api-gateway.top-parser.com/push-server-v2/"
    "?Language=ru-RU"
    "&externalPartnerId=" + PARTNER_ID +
    "&EIO=4&transport=websocket"
)

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

HTTP_HEADERS = {
    "accept": "application/json",
    "content-type": "application/json",
    "x-external-partner-id": PARTNER_ID,
    "x-lang": "ru-RU",
    "x-user-location": "RU",
    "user-agent": UA,
}

WS_HEADERS = [
    "Origin: https://" + MIRROR,
    "User-Agent: " + UA,
    "Referer: https://" + MIRROR + "/",
]

WANTED_SPORTS = [142, 47, 37, 99, 33]

PM_TO_ONEWIN_SPORT = {
    "cs2-": 142, "dota2-": 47, "lol-": 37, "valorant-": 99,
    "atp-": 33, "wta-": 33, "itf-": 33,
}

ONEWIN_SPORT_NAMES = {
    142: "CS2", 47: "DOTA2", 37: "LOL", 99: "VALORANT", 33: "TENNIS",
    136: "MOBILE_LEGENDS", 45: "R6", 170: "HEROES3",
}

_PM_GAME_RE = re.compile(r"-(?:game|map)(\d)(?:-|$)")
_PM_TOTAL_RE = re.compile(r"-(?:total|ou)-", re.I)
_PM_HANDICAP_RE = re.compile(r"-(?:handicap|spread)", re.I)
_PM_UNSUPPORTED = (
    "both-teams", "dragon", "baron", "kills-", "turret",
    "first-blood", "round-", "pistol", "exact-score",
    "odd-even", "odd_even", "map-total", "handicap-",
)

def pm_market_kind(slug):
    s = (slug or "").lower()
    for bad in _PM_UNSUPPORTED:
        if bad in s:
            return ("unsupported", None, None)
    m = _PM_GAME_RE.search(s)
    map_num = int(m.group(1)) if m else None
    if _PM_TOTAL_RE.search(s):
        return ("map_total" if map_num else "total", map_num, None)
    if _PM_HANDICAP_RE.search(s):
        return ("map_handicap" if map_num else "handicap", map_num, None)
    if map_num is not None:
        return ("map_winner", map_num, None)
    return ("winner", None, None)

_ID_RE = re.compile(
    r"^(\d+):([A-Z]):(\d+):\[(\d+),\[([^\]]*)\],\[(\d+)\],(\d+),(\d+),\[([^\]]*)\]\]$"
)

def parse_stake_id(sid):
    m = _ID_RE.match(sid or "")
    if not m:
        return {}
    line_s = m.group(5)
    line = None
    if line_s:
        try:
            line = float(line_s)
        except ValueError:
            line = None
    return {
        "provider": int(m.group(1)),
        "live": m.group(2) == "L",
        "internal_mid": int(m.group(3)),
        "mtype": int(m.group(4)),
        "line": line,
        "period": int(m.group(6)),
        "order": int(m.group(8)),
    }

class OneWinStream:
    def __init__(self, verbose=False):
        self.verbose = verbose
        self.matches = {}
        self.stakes = {}
        self.bindings = {}
        self._stop = threading.Event()
        self._ws = None
        self._ready = threading.Event()
        self.lock = threading.RLock()

    def fetch_live_matches(self, limit_per_sport=200):
        all_items = []
        seen_ids = set()
        for sport_id in WANTED_SPORTS:
            body = {"sportId": sport_id, "limit": limit_per_sport}
            try:
                r = requests.post(HTTP_MATCHES, json=body,
                                  headers=HTTP_HEADERS, timeout=15)
            except Exception as e:
                print("[1WIN] HTTP sport=%d err: %s" % (sport_id, e))
                continue
            if r.status_code != 200:
                print("[1WIN] HTTP sport=%d status=%d" % (sport_id, r.status_code))
                continue
            try:
                data = r.json()
            except Exception as e:
                print("[1WIN] JSON sport=%d err: %s" % (sport_id, e))
                continue
            items = (((data or {}).get("result") or {}).get("items")) or []
            new_items = []
            for m in items:
                mid = m.get("id")
                if mid and mid not in seen_ids:
                    seen_ids.add(mid)
                    new_items.append(m)
            svc = {}
            for m in new_items:
                s = m.get("service", "?")
                svc[s] = svc.get(s, 0) + 1
            name = ONEWIN_SPORT_NAMES.get(sport_id, "sp%d" % sport_id)
            print("[1WIN] sport=%d(%s): %d new (of %d), service=%s"
                  % (sport_id, name, len(new_items), len(items), svc))
            all_items.extend(new_items)
        print("[1WIN] TOTAL: %d unique matches" % len(all_items))
        if all_items:
            self._store_matches(all_items)
        return all_items

    def start(self):
        threading.Thread(target=self._run_ws, daemon=True).start()

    def stop(self):
        self._stop.set()

    def _run_ws(self):
        attempt = 0
        while not self._stop.is_set():
            attempt += 1
            try:
                self._connect_and_loop()
            except Exception as e:
                print("[1WIN] loop err (attempt %d): %s" % (attempt, e))
            if self._stop.is_set():
                break
            time.sleep(2)

    def _connect_and_loop(self):
        print("[1WIN] connecting to %s..." % WS_URL[:80])
        ws = websocket.create_connection(WS_URL, timeout=15,
                                         header=WS_HEADERS,
                                         origin="https://" + MIRROR)
        self._ws = ws
        print("[1WIN] WS CONNECTED")
        hs = ws.recv()
        if not hs.startswith("0"):
            print("[1WIN] unexpected handshake: %s" % hs[:200])
            ws.close()
            return
        try:
            hs_data = json.loads(hs[1:])
            ping_interval = hs_data.get("pingInterval", 25000) / 1000.0
        except Exception:
            ping_interval = 25.0
        print("[1WIN] handshake ok (pingInterval=%ss)" % ping_interval)

        ws.send("40")
        print("[1WIN] -> 40 (Socket.IO connect)")
        try:
            ws.settimeout(5)
            conn = ws.recv()
            if not conn.startswith("40"):
                print("[1WIN] no Socket.IO ack, got: %s" % conn[:200])
        except Exception as e:
            print("[1WIN] timeout waiting 40: %s" % e)

        # v4 FIX: start ping-loop BEFORE fetch_live_matches.
        # fetch does 5 sync HTTP POSTs (5-10 sec) which blocks the WS recv loop.
        # Without parallel ping, server (pingInterval=25s) kills the connection.
        ws.settimeout(2)
        self._start_ping(ws, ping_interval)

        matches = self.fetch_live_matches()
        mids = [m.get("id") for m in matches
                if isinstance(m, dict) and m.get("id")]
        mids = mids[:30]
        if mids:
            self._send_subscribe(ws, "subscribe-match-info", mids)
            # self._send_subscribe(ws, "subscribe-match-odds", mids)
            print("[1WIN] sent subscribe for %d ids" % len(mids))
        else:
            print("[1WIN] no ids - WS stays open without subscribe")

        self._ready.set()

        while not self._stop.is_set():
            try:
                msg = ws.recv()
            except websocket.WebSocketTimeoutException:
                continue
            except Exception as e:
                print("[1WIN] recv err: %s" % e)
                break
            if not msg:
                continue
            if msg == "2":
                try:
                    ws.send("3")
                except Exception:
                    pass
                continue
            if msg == "3":
                continue
            if msg.startswith("0") or msg.startswith("40"):
                continue
            if msg.startswith("41"):
                print("[1WIN] namespace disconnect (41)")
                break
            if msg.startswith("42"):
                try:
                    arr = json.loads(msg[2:])
                    self._handle_event(arr)
                except Exception as e:
                    if self.verbose:
                        print("[1WIN] parse err: %s" % e)
        try:
            ws.close()
        except Exception:
            pass
        print("[1WIN] WS disconnected")

    def _send_subscribe(self, ws, mtype, mids):
        payload = json.dumps(["subscribe", {
            "messageType": mtype,
            "data": {"matchIds": mids},
        }], separators=(",", ":"))
        ws.send("42" + payload)

    def _start_ping(self, ws, interval):
        # v4 FIX: send ping at 2x rate to survive slow fetch / busy recv loop
        safe_interval = max(5.0, interval * 0.5)

        def _loop():
            while not self._stop.is_set():
                time.sleep(safe_interval)
                try:
                    ws.send("2")
                except Exception:
                    return
        threading.Thread(target=_loop, daemon=True).start()

    def _handle_event(self, arr):
        if not isinstance(arr, list) or len(arr) < 2:
            return
        ev = arr[0]
        if ev != "u":
            return
        payload = arr[1]
        if not isinstance(payload, dict):
            return
        mt = payload.get("messageType")
        data = payload.get("data") or {}
        if mt in ("match-info", "match-info-snapshot"):
            self._on_match_info(data)
        elif mt in ("match-odds", "match-odds-snapshot"):
            self._on_match_odds(data)

    def _store_matches(self, items):
        with self.lock:
            for m in items:
                if not isinstance(m, dict):
                    continue
                mid = m.get("id")
                if not mid:
                    continue
                self.matches[mid] = {
                    "id": mid, "name": m.get("name"), "slug": m.get("slug"),
                    "startAt": m.get("startAt"),
                    "service": m.get("service"), "serviceV2": m.get("serviceV2"),
                    "sportId": m.get("sportId"), "sportTag": m.get("sportTag"),
                    "sport_slug": (m.get("sport") or {}).get("slug"),
                    "categoryId": m.get("categoryId"),
                    "category_slug": (m.get("category") or {}).get("slug"),
                    "tournamentId": m.get("tournamentId"),
                    "tournament_slug": (m.get("tournament") or {}).get("slug"),
                    "home": ((m.get("homeTeam") or {}).get("name")),
                    "away": ((m.get("awayTeam") or {}).get("name")),
                    "home_id": ((m.get("homeTeam") or {}).get("id")),
                    "away_id": ((m.get("awayTeam") or {}).get("id")),
                    "home_slug": ((m.get("homeTeam") or {}).get("slug")),
                    "away_slug": ((m.get("awayTeam") or {}).get("slug")),
                }

    def _on_match_info(self, data):
        mid = data.get("matchId")
        if mid is None:
            return
        with self.lock:
            m = self.matches.setdefault(mid, {"id": mid})
            m["service"] = data.get("service") or m.get("service")
            m["enabledOddsCount"] = data.get("enabledOddsCount")
            m["scoreBoard"] = data.get("scoreBoard")

    def _on_match_odds(self, data):
        mid = data.get("matchId")
        if mid is None:
            return
        with self.lock:
            bucket = self.stakes.setdefault(mid, {})
            for grp in data.get("oddsGroups", []) or []:
                gid = grp.get("id")
                gname = (grp.get("name") or "").strip()
                for st in grp.get("oddsList", []) or []:
                    sid = st.get("id")
                    if not sid:
                        continue
                    parsed = parse_stake_id(sid)
                    bucket[sid] = {
                        "sid": sid, "cf": st.get("cf"),
                        "outcome": st.get("outcome"), "name": st.get("name"),
                        "grp_id": gid, "grp_name": gname,
                        "status": st.get("status"), "ts": st.get("ts"),
                        "render": st.get("renderType") or grp.get("renderType"),
                        **parsed,
                    }

    def _pm_sport_id(self, slug):
        s = (slug or "").lower()
        for pref, sid in PM_TO_ONEWIN_SPORT.items():
            if s.startswith(pref):
                return sid
        return None

    def _name_score(self, a, b):
        if not a or not b:
            return 0.0
        a = a.lower().strip()
        b = b.lower().strip()
        if a == b:
            return 1.0
        if a in b or b in a:
            return 0.85

        def tri(s):
            s = "  " + s + "  "
            return {s[i:i+3] for i in range(len(s) - 2)}
        A, B = tri(a), tri(b)
        if not A or not B:
            return 0.0
        return len(A & B) / max(len(A), len(B))

    def bind(self, slug, pm_teams):
        kind, arg, line = pm_market_kind(slug)
        if kind == "unsupported":
            return None
        sport_id = self._pm_sport_id(slug)
        if sport_id is None:
            return None
        if not pm_teams or len(pm_teams) != 2:
            return None
        with self.lock:
            matches = list(self.matches.values())
        best, best_score = None, 0.0
        for m in matches:
            if m.get("sportId") != sport_id:
                continue
            h, a = m.get("home"), m.get("away")
            if not h or not a:
                continue
            sc = max(
                min(self._name_score(pm_teams[0], h),
                    self._name_score(pm_teams[1], a)),
                min(self._name_score(pm_teams[0], a),
                    self._name_score(pm_teams[1], h)),
            )
            if sc > best_score:
                best, best_score = m, sc
        min_score = 0.65 if sport_id == 33 else 0.75
        if not best or best_score < min_score:
            return None
        pm_h, pm_a = pm_teams[0], pm_teams[1]
        h, a = best.get("home"), best.get("away")
        direct = (self._name_score(pm_h, h) + self._name_score(pm_a, a)) / 2
        swapped = (self._name_score(pm_h, a) + self._name_score(pm_a, h)) / 2
        swap = swapped > direct
        binding = {
            "match_id": best["id"], "sport_id": sport_id,
            "kind": kind, "arg": arg, "line": line,
            "score": best_score, "swap": swap,
            "home": h, "away": a, "pm_home": pm_h, "pm_away": pm_a,
            "bound_at": time.time(),
        }
        with self.lock:
            self.bindings[slug] = binding
        return binding

    def _mtype_period_for(self, kind, arg):
        if kind == "winner":
            return (1, 0)
        if kind == "map_winner":
            return (1, arg or 0)
        if kind == "total":
            return (264, 0)
        if kind == "map_total":
            return (5, arg or 0)
        if kind == "handicap":
            return (260, 0)
        if kind == "map_handicap":
            return (4, arg or 0)
        return (None, None)

    def live_bc(self, slug):
        with self.lock:
            b = self.bindings.get(slug)
            if not b:
                return {}
            stakes = dict(self.stakes.get(b["match_id"]) or {})
        mtype, period = self._mtype_period_for(b["kind"], b["arg"])
        if mtype is None:
            return {}
        pm_h, pm_a = b.get("pm_home"), b.get("pm_away")
        out = {}
        for sid, st in stakes.items():
            if st.get("mtype") != mtype or st.get("period") != period:
                continue
            oc = st.get("outcome")
            if oc not in ("1", "2"):
                continue
            cf = st.get("cf")
            if not cf or cf <= 1.0:
                continue
            if b["swap"]:
                side = pm_a if oc == "1" else pm_h
            else:
                side = pm_h if oc == "1" else pm_a
            if side:
                out[side] = {"src": "1win", "o": float(cf), "c": 0.0}
        return out

    def is_ready(self):
        with self.lock:
            return len(self.matches) > 0

    def snapshot_matches(self):
        with self.lock:
            return list(self.matches.values())


# ============================================================
# MODULE-LEVEL FACADE (for bk_router)
# ============================================================

_GLOBAL = None
_GLOBAL_LOCK = threading.Lock()


def get_global_stream(verbose=False):
    global _GLOBAL
    with _GLOBAL_LOCK:
        if _GLOBAL is None:
            _GLOBAL = OneWinStream(verbose=verbose)
            _GLOBAL.start()
        return _GLOBAL


def stop_global_stream():
    global _GLOBAL
    with _GLOBAL_LOCK:
        if _GLOBAL is not None:
            _GLOBAL.stop()
            _GLOBAL = None


def wait_ready(timeout=120, min_matches=1, poll=0.5):
    s = get_global_stream()
    if s is None:
        return False
    t0 = time.time()
    last_log = 0
    while time.time() - t0 < timeout:
        n = len(s.matches)
        if n >= min_matches:
            print("    [1win wait_ready] matches=%d за %dс"
                  % (n, int(time.time() - t0)))
            return True
        if time.time() - last_log >= 5:
            print("    [1win wait_ready] %d матчей... (нужно %d)"
                  % (n, min_matches))
            last_log = time.time()
        time.sleep(poll)
    return False


def bind_slug(slug, pm_teams):
    s = get_global_stream()
    if s is None:
        return None
    try:
        return s.bind(slug, pm_teams)
    except Exception:
        return None


def get_binding(slug):
    s = get_global_stream()
    if s is None:
        return None
    with s.lock:
        b = s.bindings.get(slug)
        return dict(b) if b else None


def get_live_bc(slug):
    s = get_global_stream()
    if s is None:
        return {}
    try:
        return s.live_bc(slug)
    except Exception:
        return {}


def smoke_test(verbose=False):
    print("[1WIN] SMOKE TEST")
    s = get_global_stream(verbose=verbose)
    if not wait_ready(timeout=45, min_matches=10):
        print("[1WIN] not ready")
        return
    time.sleep(3)
    with s.lock:
        nm = len(s.matches)
        ns = sum(len(v) for v in s.stakes.values())
    print("[1WIN] matches=%d stakes=%d" % (nm, ns))
    tests = []
    pref_map = {142: "cs2", 47: "dota2", 37: "lol", 99: "valorant", 33: "wta"}
    with s.lock:
        for m in list(s.matches.values()):
            if (m.get("sportId") in (142, 47, 37, 99, 33)
                    and m.get("home") and m.get("away")):
                sid = m.get("sportId")
                pref = pref_map[sid]
                slug = ("%s-%s-%s-2026-10-04"
                        % (pref, m.get("home_slug", "x"),
                           m.get("away_slug", "y")))
                tests.append((slug, [m.get("home"), m.get("away")]))
                if len(tests) >= 6:
                    break
    for slug, teams in tests:
        b = bind_slug(slug, teams)
        if b:
            bc = get_live_bc(slug)
            print("[1WIN] [OK] %s -> mid=%s %s score=%.2f"
                  % (slug, b["match_id"], b["kind"], b["score"]))
            print("[1WIN]      bc=%s" % bc)
        else:
            print("[1WIN] [NONE] %s" % slug)
    stop_global_stream()
    print("[1WIN] done")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--verbose", "-v", action="store_true")
    args = ap.parse_args()
    smoke_test(verbose=args.verbose)


if __name__ == "__main__":
    main()