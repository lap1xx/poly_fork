# bb_bridge.py - BetBoom tree_ws <-> POLY-ARB bridge. v10.
# v10: работает через chrome_bridge.py на ws://127.0.0.1:8765/ (обход TLS/JA3).
# v6: _winner_side определяет исход по URL если display_name/name пустые.
# v5: parse_stake всегда проставляет st["map_num"]; универсальный walker для f29/f30/f17.
from __future__ import annotations
import json, re, struct, threading, time, uuid
from datetime import datetime, timezone

try:
    import websocket
except ImportError:
    websocket = None

BB_URL = "ws://127.0.0.1:8765/"
BB_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
BB_ORIGIN = "https://betboom.ru"
BB_REFERER = "https://betboom.ru/"
D_SPORT = 1
D_CYBER = 2
SPORT_TENNIS = 4
SPORT_ESPORTS = 1
LIVE = 1
PREMATCH = 2
DEFAULT_SUBS = [(D_SPORT, SPORT_TENNIS, "TENNIS"),(D_CYBER, SPORT_ESPORTS, "CYBER")]

_PM_TOTAL_RE = re.compile(r"-(?:total|match-total|total-games|total-maps)-(\d+)pt(\d+)", re.I)
_PM_HANDICAP_RE = re.compile(r"-(?:game-handicap|map-handicap|handicap|spread)-(-?\d+)pt(\d+)", re.I)
_PM_GAME_RE = re.compile(r"-(?:game|map)(\d)(?:-|$)", re.I)
_STOPWORDS = {"over", "under", "yes", "no", "o", "u", "1", "2"}

_ODDIN_MAP_RE       = re.compile(r"[?&]map=(\d+)")
_ODDIN_ROUND_RE     = re.compile(r"[?&]round=(\d+)")
_ODDIN_HANDICAP_RE  = re.compile(r"[?&]handicap=(-?\d+(?:\.\d+)?)")
_ODDIN_THRESHOLD_RE = re.compile(r"[?&]threshold=(-?\d+(?:\.\d+)?)")
_MAP_NUM_IN_PERIOD  = re.compile(r"(\d+)")
_MAP_SLUG_RE        = re.compile(r"map-(\d+)", re.I)
_MAP_NUM_IN_MARKET  = re.compile(r"(?:\bmap[\s\-]+(\d+)\b)|(?:\b(\d+)[\s\-]+map\b)", re.I)
_COMPOSITE_MAP_RE   = re.compile(r"map[-_](\d+)", re.I)

_URL_OUTCOME_HOME = {"1", "4", "70", "74", "1714", "2047", "882", "884", "886", "888", "890", "892"}
_URL_OUTCOME_AWAY = {"2", "5", "72", "76", "1715", "2048", "883", "885", "887", "889", "891", "893"}

def _stake_map_num(st):
    url = st.get("url") or ""
    m = _ODDIN_MAP_RE.search(url)
    if m:
        try: return int(m.group(1))
        except Exception: pass
    pn = st.get("period_name") or st.get("period_name_alt") or ""
    low = pn.lower()
    if "карт" in low or "map" in low:
        m = _MAP_NUM_IN_PERIOD.search(pn)
        if m:
            try: return int(m.group(1))
            except Exception: pass
    mn = st.get("market_name") or ""
    m = _MAP_NUM_IN_MARKET.search(mn)
    if m:
        try: return int(m.group(1) or m.group(2))
        except Exception: pass
    cid = st.get("composite_id") or ""
    m = _COMPOSITE_MAP_RE.search(cid)
    if m:
        try: return int(m.group(1))
        except Exception: pass
    slug = st.get("market_slug") or ""
    m = _MAP_SLUG_RE.search(slug)
    if m:
        try: return int(m.group(1))
        except Exception: pass
    return None

def _stake_line(st):
    if st.get("line") is not None:
        return st["line"]
    url = st.get("url") or ""
    m = _ODDIN_HANDICAP_RE.search(url) or _ODDIN_THRESHOLD_RE.search(url)
    if m:
        try: return float(m.group(1))
        except Exception: pass
    return None

def _outcomes_look_like_names(outcomes):
    if not outcomes:
        return False
    for o in outcomes:
        if not o or o.lower().strip() in _STOPWORDS:
            return False
    return True

def slug_to_team_tokens(slug):
    s = (slug or "").lower()
    for pref in ("cs2-", "dota2-", "lol-", "valorant-", "atp-", "wta-", "itf-"):
        if s.startswith(pref):
            s = s[len(pref):]
            break
    s = re.sub(r"-\d{4}-\d{2}-\d{2}.*$", "", s)
    parts = [p for p in s.split("-") if p]
    if len(parts) < 2:
        return None, None
    mid = len(parts) // 2
    return "-".join(parts[:mid]), "-".join(parts[mid:])

def vint(x):
    out = b""
    while True:
        b = x & 0x7f
        x >>= 7
        out += bytes([b | 0x80 if x else b])
        if not x:
            break
    return out

def tag(f, wt):
    return vint((f << 3) | wt)

def s(f, v):
    b = v.encode()
    return tag(f, 2) + vint(len(b)) + b

def i64(f, v):
    return tag(f, 0) + vint(v)

def enum(f, v):
    return tag(f, 0) + vint(v)

def msg(f, p):
    return tag(f, 2) + vint(len(p)) + p

def req_settings():
    return msg(3, enum(1, 8) + s(2, "1_hours"))

def req_f4(domain, types):
    return msg(4, enum(1, domain) + b"".join(enum(2, t) for t in types))

def req_f6(sport_id, ttype=LIVE):
    uid = uuid.uuid4().hex[:6]
    inner = s(1, uid) + enum(2, ttype) + i64(3, sport_id)
    return msg(6, s(1, uid) + msg(2, inner))

def rv(b, i=0):
    r, sh = 0, 0
    while i < len(b):
        x = b[i]
        r |= (x & 0x7f) << sh
        i += 1
        if not (x & 0x80):
            return r, i
        sh += 7
    raise ValueError("trunc")

def pf(b):
    i = 0
    while i < len(b):
        t, i = rv(b, i)
        fn, wt = t >> 3, t & 7
        if wt == 0:
            v, i = rv(b, i)
            yield fn, wt, v
        elif wt == 2:
            ln, i = rv(b, i)
            if i + ln > len(b):
                return
            yield fn, wt, b[i:i + ln]
            i += ln
        elif wt == 1:
            if i + 8 > len(b):
                return
            yield fn, wt, b[i:i + 8]
            i += 8
        elif wt == 5:
            if i + 4 > len(b):
                return
            yield fn, wt, b[i:i + 4]
            i += 4
        else:
            return

def top_field(b):
    try:
        return rv(b, 0)[0] >> 3
    except Exception:
        return None

def top_payload(b):
    try:
        t, i = rv(b, 0)
        fn, wt = t >> 3, t & 7
        if wt != 2:
            return None
        ln, i = rv(b, i)
        return b[i:i + ln]
    except Exception:
        return None

def try_str(raw):
    try:
        v = raw.decode("utf-8")
        if v and all(c.isprintable() or c in "\t\n" for c in v):
            return v
    except Exception:
        pass
    return None

def parse_match(raw):
    m = {}
    teams = []
    for fn, wt, v in pf(raw):
        if wt == 0:
            if fn == 1: m["match_id"] = v
            elif fn == 8: m["f8_sport"] = v
            elif fn == 9: m["f9_cat"] = v
            elif fn == 10: m["f10"] = v
        elif wt == 2:
            if fn == 5:
                ss = try_str(v)
                if ss: m["sr_match"] = ss
            elif fn == 13:
                ss = try_str(v)
                if ss: m["start_dttm"] = ss
            elif fn == 16:
                for fn2, wt2, v2 in pf(v):
                    if wt2 != 2: continue
                    team = {}
                    for fn3, wt3, v3 in pf(v2):
                        if fn3 == 1 and wt3 == 0: team["id"] = v3
                        elif fn3 == 3 and wt3 == 2:
                            ss = try_str(v3)
                            if ss: team["name"] = ss
                        elif fn3 == 4 and wt3 == 2:
                            ss = try_str(v3)
                            if ss: team["short_name"] = ss
                    if team: teams.append(team)
            elif fn == 20:
                for fn2, wt2, v2 in pf(v):
                    if fn2 == 2 and wt2 == 2:
                        ss = try_str(v2)
                        if ss: m["score"] = ss
    if teams: m["teams"] = teams
    return m

def parse_stake(raw):
    st = {}
    for fn, wt, v in pf(raw):
        if wt == 0:
            if fn == 2: st["match_id"] = v
            elif fn == 11: st["is_active"] = bool(v)
            elif fn == 13: st["market_id"] = v
            elif fn == 17: st["order"] = v
        elif wt == 1:
            if fn == 9:
                try: st["line"] = struct.unpack("<d", v)[0]
                except Exception: pass
            elif fn == 10:
                try: st["factor"] = struct.unpack("<d", v)[0]
                except Exception: pass
        elif wt == 2:
            if fn == 1:
                ss = try_str(v)
                if ss:
                    st["stake_id"] = ss
                    st["url"]      = ss
            elif fn == 5:
                ss = try_str(v)
                if ss: st["name"] = ss
            elif fn == 6:
                ss = try_str(v)
                if ss: st["short_name"] = ss
            elif fn == 14:
                ss = try_str(v)
                if ss: st["market_name"] = ss
            elif fn == 16:
                ss = try_str(v)
                if ss: st["market_view"] = ss
            elif fn == 18:
                ss = try_str(v)
                if ss: st["f18"] = ss
            elif fn == 20:
                ss = try_str(v)
                if ss: st["period_name"] = ss
            elif fn == 22:
                ss = try_str(v)
                if ss: st["market_slug"] = ss
            elif fn == 24:
                ss = try_str(v)
                if ss: st["composite_id"] = ss
            elif fn == 25:
                ss = try_str(v)
                if ss: st["display_name"] = ss
    if "period_name" not in st and "f18" in st:
        st["period_name"] = st["f18"]
    st["map_num"] = _stake_map_num(st)
    return st

def parse_f29(payload):
    match = None
    stakes = []
    for fn, wt, v in pf(payload):
        if fn != 6 or wt != 2: continue
        for fn2, wt2, v2 in pf(v):
            if fn2 == 1 and wt2 == 2:
                match = parse_match(v2)
            elif fn2 == 2 and wt2 == 2:
                st = parse_stake(v2)
                if "factor" in st: stakes.append(st)
    return match, stakes

def _try_extract_match_stakes(buf):
    match = None
    stakes = []
    try:
        for fn, wt, v in pf(buf):
            if wt != 2:
                continue
            if fn == 1:
                m = parse_match(v)
                if m.get("match_id") and m.get("sr_match"):
                    match = m
            elif fn == 2:
                st = parse_stake(v)
                if st.get("factor") is not None and st.get("url"):
                    stakes.append(st)
    except Exception:
        return None, []
    if match and stakes:
        return match, stakes
    return None, []

def _walk_find_match_and_stakes(buf, depth=0, max_depth=15):
    if depth > max_depth or not buf:
        return None, []
    m, sts = _try_extract_match_stakes(buf)
    if m:
        return m, sts
    try:
        for fn, wt, v in pf(buf):
            if wt != 2:
                continue
            if len(v) < 8:
                continue
            m, sts = _walk_find_match_and_stakes(v, depth + 1, max_depth)
            if m:
                return m, sts
    except Exception:
        pass
    return None, []

def _parse_iso(s):
    if not s: return None
    try: return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception: return None

def _slug_date(slug):
    m = re.search(r"(\d{4}-\d{2}-\d{2})", slug or "")
    if not m: return None
    try: return datetime.strptime(m.group(1), "%Y-%m-%d").date()
    except Exception: return None

def _slug_sport_hint(slug):
    s = (slug or "").lower()
    if s.startswith(("atp-", "wta-", "itf-", "tennis-")): return "TENNIS"
    if s.startswith(("cs2-", "dota2-", "lol-", "valorant-")): return "CYBER"
    return None

_TOKEN_RE = re.compile(r"[a-z0-9]+")
_TEAM_STOPWORDS = {"esports","e","sports","team","gaming","academy","club","stars","gg","roster","squad","pro","lol","cs2","dota2","dota"}

def _tokens(x):
    if not x: return set()
    return set(_TOKEN_RE.findall(x.lower())) - _TEAM_STOPWORDS

def name_match_score(pm_name, bb_name):
    if not pm_name or not bb_name: return 0.0
    a = _tokens(pm_name); b = _tokens(bb_name)
    if not a or not b: return 0.0
    if a == b: return 1.0
    common = a & b
    if not common: return 0.0
    pm_long = max(a, key=len); bb_long = max(b, key=len)
    if pm_long in b and bb_long in a: return 1.0
    if pm_long in b or bb_long in a:
        shorter = min(len(pm_long), len(bb_long))
        return 0.88 if shorter >= 4 else 0.75
    overlap = len(common) / max(len(a), len(b))
    return 0.7 if overlap >= 0.5 else 0.5

def team_match_score(pm_name, bb_team):
    if not bb_team: return 0.0
    best = 0.0
    for key in ("name", "short_name"):
        v = bb_team.get(key)
        if not v: continue
        sc = name_match_score(pm_name, v)
        if sc > best: best = sc
    return best

def match_pm_to_bb(pm_market, bb_matches, min_score=0.7, date_window_days=1):
    pm_o = pm_market.get("o") or []
    slug = pm_market.get("slug") or ""
    if len(pm_o) != 2: return None, 0.0
    if _outcomes_look_like_names(pm_o):
        match_names = pm_o
    else:
        t1, t2 = slug_to_team_tokens(slug)
        if not t1 or not t2: return None, 0.0
        match_names = [t1, t2]
    sport_hint = _slug_sport_hint(slug)
    # Для кибера — жёстче порог (0.85), теннис — обычный (0.7)
    effective_min = 0.85 if sport_hint == "CYBER" else min_score
    pm_date = _slug_date(slug)
    if not pm_date and pm_market.get("start"):
        dt = _parse_iso(pm_market["start"])
        if dt: pm_date = dt.date()
    best, best_score = None, 0.0
    for bm in bb_matches:
        bb_hint = bm.get("_label")
        f8 = bm.get("f8_sport")
        if f8 == 4:   bb_hint = "TENNIS"
        elif f8 == 1: bb_hint = "CYBER"
        if sport_hint and bb_hint and bb_hint != sport_hint: continue
        bb_dt = _parse_iso(bm.get("start_dttm") or "")
        bb_date = bb_dt.date() if bb_dt else None
        if pm_date and bb_date and abs((bb_date - pm_date).days) > date_window_days: continue
        teams = bm.get("teams") or []
        if len(teams) < 2: continue
        s_a  = team_match_score(match_names[0], teams[0])
        s_b  = team_match_score(match_names[1], teams[1])
        s_as = team_match_score(match_names[0], teams[1])
        s_bs = team_match_score(match_names[1], teams[0])
        score = max(min(s_a, s_b), min(s_as, s_bs))
        if score > best_score:
            best_score, best = score, bm
    return (best, best_score) if best_score >= effective_min else (None, 0.0)

_PM_UNSUPPORTED = (
    "-both-teams", "-kill-", "-first-set-", "-set-totals",
    "-round-", "-dragon", "-baron", "-turret", "-player-",
    "-quadra", "-penta", "-any-player", "-handicap-home-",
    "-handicap-away-", "-set-handicap-", "-set-winner",
    "-first-map", "-series-", "-correct-score",
)

def pm_market_kind(slug):
    s = (slug or "").lower()
    m = _PM_GAME_RE.search(s)
    if m:
        try: n = int(m.group(1))
        except Exception: n = None
        return ("map_winner", n, None)
    if any(kw in s for kw in _PM_UNSUPPORTED):
        return ("unsupported", None, None)
    m = _PM_TOTAL_RE.search(s)
    if m:
        try: arg = float(f"{m.group(1)}.{m.group(2)}")
        except Exception: arg = None
        return ("total", arg, None)
    m = _PM_HANDICAP_RE.search(s)
    if m:
        try: arg = float(f"{m.group(1)}.{m.group(2)}")
        except Exception: arg = None
        return ("handicap", arg, None)
    return ("winner", None, None)

def _arg_from_display(name):
    if not name: return None
    m = re.search(r"\(([-+]?\d+\.?\d*)\)", name)
    if not m:         return None
    try: return float(m.group(1))
    except Exception: return None


def _is_winner_stake(st):
    mn = (st.get("market_name") or "").lower()
    pn = (st.get("period_name") or "").lower()
    if any(k in mn for k in ("map", "карт", "set", "game", "game ")):
        return False
    if any(ch.isdigit() for ch in mn):
        return False
    if not any(k in mn for k in ("winner", "исход", "match winner", "1x2")):
        return False
    if pn and pn not in ("match", "матч", ""):
        return False
    return True

def _is_total_stake(st):
    mn = (st.get("market_name") or "").lower()
    return "total" in mn or "тотал" in mn

def _is_handicap_stake(st):
    mn = (st.get("market_name") or "").lower()
    return "handicap" in mn or "фора" in mn

def _is_map_winner_stake(st):
    url = (st.get("url") or "").lower()
    mn  = (st.get("market_name") or "").lower()
    n = st.get("map_num") or _stake_map_num(st)
    if not n:
        return False
    if "handicap=" in url or "threshold=" in url or "round=" in url:
        return False
    if any(k in mn for k in ("handicap", "фора", "total", "тотал",
                             "round", "раунд", "kills", "kills ")):
        return False
    if " / " in mn or "(threeway)" in mn or "threeway" in mn:
        return False
    if any(k in mn for k in ("победа на карте", "победитель карты",
                             "map winner", "победит в карте")):
        return True
    if "?map=" in url:
        return True
    pn = (st.get("period_name") or "").lower()
    if f"карта {n}" in pn and any(k in mn for k in ("исход", "winner", "1x2")):
        return True
    return False

def _is_map_total_stake(st, map_num=None):
    url = (st.get("url") or "").lower()
    mn  = (st.get("market_name") or "").lower()
    n = st.get("map_num") or _stake_map_num(st)
    if n is None: return False
    if map_num is not None and n != map_num: return False
    if "map=" in url and ("threshold=" in url or "handicap=" in url):
        return True
    pn = (st.get("period_name") or "").lower()
    if ("total" in mn or "тотал" in mn) and f"карта {n}" in pn:
        return True
    return False

def _is_map_handicap_stake(st, map_num=None):
    url = (st.get("url") or "").lower()
    mn  = (st.get("market_name") or "").lower()
    n = st.get("map_num") or _stake_map_num(st)
    if n is None: return False
    if map_num is not None and n != map_num: return False
    if "map=" in url and "handicap=" in url:
        return True
    pn = (st.get("period_name") or "").lower()
    if ("handicap" in mn or "фора" in mn) and f"карта {n}" in pn:
        return True
    return False

def _winner_side(st):
    for key in ("display_name", "name", "short_name"):
        v = (st.get(key) or "").strip().lower()
        if not v:
            continue
        if v in ("win1", "п1", "1", "t1", "home", "w1"):
            return "home"
        if v in ("win2", "п2", "2", "t2", "away", "w2"):
            return "away"
    url = (st.get("url") or "")
    path = url.split("?", 1)[0]
    parts = re.findall(r"/([0-9]+)", path)
    if parts:
        oid = parts[-1]
        if oid in _URL_OUTCOME_HOME:
            return "home"
        if oid in _URL_OUTCOME_AWAY:
            return "away"
    return None

def _extract_winner(stakes, teams=None):
    sides = {"home": [], "away": []}
    for st in stakes:
        if not _is_winner_stake(st):
            continue
        if not st.get("is_active", True):
            continue
        side = _winner_side(st)
        if side in sides:
            sides[side].append(st)
    # Do not infer outcome identity from list order. Ambiguous or duplicate
    # selections fail closed instead of producing a potentially false signal.
    if len(sides["home"]) != 1 or len(sides["away"]) != 1:
        return None
    return {"home": sides["home"][0], "away": sides["away"][0]}

def _extract_map_winner(stakes, map_num):
    if not map_num:
        return None
    sides = {"home": [], "away": []}
    for st in stakes:
        if not st.get("is_active", True):
            continue
        if not _is_map_winner_stake(st):
            continue
        if (st.get("map_num") or _stake_map_num(st)) != map_num:
            continue
        side = _winner_side(st)
        if side in sides:
            sides[side].append(st)
    if len(sides["home"]) != 1 or len(sides["away"]) != 1:
        return None
    return {"home": sides["home"][0], "away": sides["away"][0]}

def _extract_total(stakes, arg):
    if arg is None: return None
    over = under = None
    for st in stakes:
        if not _is_total_stake(st): continue
        if not st.get("is_active", True): continue
        st_arg = _arg_from_display(st.get("display_name")) or _arg_from_display(st.get("name"))
        if st_arg is None: continue
        if abs(st_arg - arg) > 0.01: continue
        dn = (st.get("display_name") or st.get("name") or "").lower()
        if dn.startswith(("over", "б", "больше")): over = over or st
        elif dn.startswith(("under", "м", "меньше")): under = under or st
    if over and under: return {"over": over, "under": under}
    return None

def _extract_handicap(stakes, arg):
    if arg is None: return None
    sides = {}
    for st in stakes:
        if not _is_handicap_stake(st): continue
        if not st.get("is_active", True): continue
        st_arg = _arg_from_display(st.get("display_name")) or _arg_from_display(st.get("name"))
        if st_arg is None: continue
        if abs(abs(st_arg) - abs(arg)) > 0.01: continue
        key = "+" if st_arg > 0 else "-"
        sides[key] = st
    if len(sides) >= 2: return sides
    return None

class BBStream:
    def __init__(self, subs=None, verbose=False):
        self.subs = subs or list(DEFAULT_SUBS)
        self.verbose = verbose
        self.lock = threading.RLock()
        self.matches = {}
        self._ws_by_label = {}
        self.stakes = {}
        self.bindings = {}
        self._stop = threading.Event()
        self._threads = []
        self._f16_subs = set()
        self._seen_fns = set()
        self._last_sub_sent = 0.0

    def start(self):
        if websocket is None:
            self.log("[BB] websocket-client not installed - skip")
            return
        for domain, sid, label in self.subs:
            t = threading.Thread(target=self._run_loop, args=(domain, sid, label), daemon=True)
            t.start()
            self._threads.append(t)

    def stop(self):
        self._stop.set()

    def log(self, *a):
        if self.verbose: print(*a, flush=True)

    def _run_loop(self, domain, sport_id, label):
        backoff = 2.0
        while not self._stop.is_set():
            try:
                self._connect_and_listen(domain, sport_id, label)
                backoff = 2.0
            except Exception as e:
                print(f"[BB {label}] LOOP-ERR {type(e).__name__}: {e}", flush=True)
                time.sleep(backoff)
                backoff = min(backoff * 1.6, 20.0)

    def _connect_and_listen(self, domain, sport_id, label):
        print(f"[BB {label}] connecting to {BB_URL} ...", flush=True)
        ws = websocket.create_connection(
            BB_URL, timeout=10,
            origin=BB_ORIGIN,
            header={"User-Agent": BB_UA, "Referer": BB_REFERER},
        )
        try:
            with self.lock:
                self._ws_by_label[label] = ws
            print(f"[BB {label}] CONNECTED (domain={domain}, sport={sport_id})", flush=True)
            ws.settimeout(2.0)
            def _do_subscribe():
                try:
                    ws.send_binary(req_settings())
                    time.sleep(0.2)
                    ws.send_binary(req_f4(domain, [LIVE]))
                    time.sleep(0.4)
                    p6 = req_f6(sport_id, LIVE)
                    ws.send_binary(p6)
                    ws.send_binary(req_f6(sport_id, PREMATCH))
                    # print(f"[BB {label}] SUBSCRIBED (domain={domain}, sport={sport_id}) f6_hex={p6.hex()}", flush=True)
                    return True
                except Exception as e:
                    print(f"[BB {label}] subscribe send FAILED: {type(e).__name__}: {e}", flush=True)
                    return False

            _do_subscribe()
            self._last_sub_sent = time.time()
            RESUB_EVERY = 8.0
            while not self._stop.is_set():
                try:
                    r = ws.recv()
                except websocket.WebSocketTimeoutException:
                    if time.time() - self._last_sub_sent > RESUB_EVERY:
                        # print(f"[BB {label}] resubscribe (timer)", flush=True)
                        _do_subscribe()
                        self._last_sub_sent = time.time()
                    continue
                except websocket.WebSocketConnectionClosedException as e:
                    print(f"[BB {label}] conn closed: {e}", flush=True)
                    break
                except Exception as e:
                    print(f"[BB {label}] recv err: {type(e).__name__}: {e}", flush=True)
                    break
                if not r or isinstance(r, str):
                    continue
                fn = top_field(r)
                # print(f"[RAW {label}] fn={fn} len={len(r)}", flush=True)
                if len(r) > 500 and fn != 5:
                    # print(f"[BB {label}] big fn={fn} -> _handle_f30 ({len(r)}B)", flush=True)
                    try:
                        self._handle_f30(bytes(r), label)
                    except Exception as _e:
                        print(f"[BB {label}] big err: {_e}", flush=True)
                    continue
            if fn not in self._seen_fns:
                self._seen_fns.add(fn)
                # print(f"[HEX {label}] fn={fn} len={len(r)} hex={bytes(r)[:120].hex()}", flush=True)
            _now = time.time()
            if _now - self._last_sub_sent > 0.5:
                try:
                    ws.send_binary(req_f6(sport_id, LIVE))
                    ws.send_binary(req_f6(sport_id, PREMATCH))
                    self._last_sub_sent = _now
                    # print(f"[BB {label}] RESUB (any-frame) after fn={fn}", flush=True)
                except Exception as _e:
                    print(f"[BB {label}] resub fail: {_e}", flush=True)
                if fn == 5:
                    # tree arrived - Chrome hook is alive, resend subscribe
                    now = time.time()
                    if now - self._last_sub_sent > 1.0:
                        try:
                            ws.send_binary(req_f6(sport_id, LIVE))
                            ws.send_binary(req_f6(sport_id, PREMATCH))
                            self._last_sub_sent = now
                            print(f"[BB {label}] RESUB on fn=5", flush=True)
                        except Exception as e:
                            print(f"[BB {label}] resub fn=5 fail: {e}", flush=True)
                elif fn == 29:
                    self._handle_f29(bytes(r), label)
                elif fn == 7 and len(r) > 500:
                    print(f"[BB {label}] f7-BIG -> _handle_f29 ({len(r)}B)", flush=True)
                    self._handle_f29(bytes(r), label)
                elif fn == 30:
                    self._handle_f30(bytes(r), label)
                elif fn == 31:
                    self._handle_f31(bytes(r), label)
                elif fn == 17:
                    self._handle_f17(bytes(r), label)
        finally:
            with self.lock:
                self._ws_by_label.pop(label, None)
            try: ws.close()
            except Exception: pass

    def _handle_f29(self, raw, label):
        pl = top_payload(raw)
        if not pl:
            print(f"[H29 {label}] NO PAYLOAD", flush=True)
            return
        try:
            match, stakes = parse_f29(pl)
        except Exception as e:
            print(f"[H29 {label}] PARSE ERR: {type(e).__name__}: {e}", flush=True)
            return
        if match:
            mid = match.get("match_id")
            teams = match.get("teams") or []
            print(f"[H29 {label}] match={mid} teams={len(teams)} stakes={len(stakes)}", flush=True)
        else:
            # диагностика: что вообще внутри pl?
            flds = [fn for fn, wt, v in pf(pl)][:10]
            print(f"[H29 {label}] NO MATCH, top-fields={flds} pl_len={len(pl)}", flush=True)
        self._store_f29_result(match, stakes, label)

    def _handle_f30(self, raw, label):
        pl = top_payload(raw)
        if not pl: return
        try: match, stakes = _walk_find_match_and_stakes(pl)
        except Exception as e:
            self.log(f"[BB {label}] F30 parse error: {e}")
            return
        self.log(f"[BB {label}] F30: match={match.get('match_id') if match else None}, "
                 f"stakes={len(stakes)}")
        self._store_f29_result(match, stakes, label)

    def _handle_f17(self, raw, label):
        pl = top_payload(raw)
        if not pl: return
        try: match, stakes = _walk_find_match_and_stakes(pl)
        except Exception as e:
            self.log(f"[BB {label}] F17 parse error: {e}")
            return
        if match or stakes:
            self.log(f"[BB {label}] F17: match={match.get('match_id') if match else None}, "
                     f"stakes={len(stakes)}, pl={len(pl)}")
            self._store_f29_result(match, stakes, label)

    def _handle_f31(self, raw, label):
        pl = top_payload(raw)
        if not pl: return
        try: match, stakes = parse_f29(pl)
        except Exception: return
        self._store_f29_result(match, stakes, label)

    _KEEP_F8 = {1, 4}   # 1=Esports, 4=Tennis

    def _store_f29_result(self, match, stakes, label):
        now = time.time()
        with self.lock:
            if match and "match_id" in match:
                # фильтр: только Esports (1) и Tennis (4)
                # if match.get("f8_sport") not in self._KEEP_F8:
                #     return
                match["_label"] = label
                match["_ts"] = now
                self.matches[match["match_id"]] = match
            if stakes:
                fallback_mid = match.get("match_id") if match else None
                for st in stakes:
                    mid = st.get("match_id") or fallback_mid
                    if not mid:
                        continue
                    st["match_id"] = mid
                    bucket = self.stakes.setdefault(mid, {})
                    sid = st.get("stake_id")
                    if sid:
                        st["_ts"] = now
                        bucket[sid] = st

    def subscribe_f16(self, match_id):
        uid  = uuid.uuid4().hex[:6]
        uid2 = uuid.uuid4().hex[:6]
        inner = s(1, uid2) + i64(2, match_id)
        payload = msg(16, s(1, uid) + msg(2, inner))
        with self.lock:
            ws_list = list(self._ws_by_label.values())
        for ws in ws_list:
            try: ws.send_binary(payload)
            except Exception: pass

    def snapshot_matches(self):
        with self.lock: return list(self.matches.values())

    def snapshot_stakes(self, mid):
        with self.lock:
            bucket = self.stakes.get(mid) or {}
            return list(bucket.values())

    def is_ready(self):
        with self.lock: return len(self.matches) > 0

    def bind(self, slug, pm_market):
        kind, arg, _ = pm_market_kind(slug)
        if kind == "unsupported":
            return None
        bb, score = match_pm_to_bb(pm_market, self.snapshot_matches())
        if not bb: return None
        teams = bb.get("teams") or []
        if len(teams) < 2: return None
        pm_o = pm_market.get("o") or []
        if len(pm_o) != 2: return None
        s0_home = team_match_score(pm_o[0], teams[0])
        s0_away = team_match_score(pm_o[0], teams[1])
        if s0_home >= s0_away:
            mapping = {pm_o[0]: "home", pm_o[1]: "away"}
        else:
            mapping = {pm_o[0]: "away", pm_o[1]: "home"}
        binding = {
            "match_id": bb["match_id"], "kind": kind, "arg": arg,
            "map": mapping, "score": score, "bound_at": time.time(),
            "tournament_id": bb.get("f10"),
            "sport_id": bb.get("f8_sport"),
        }
        with self.lock:
            self.bindings[slug] = binding
        if kind == "map_winner" and bb["match_id"] not in self._f16_subs:
            self._f16_subs.add(bb["match_id"])
            try:
                self.subscribe_f16(bb["match_id"])
                self.log(f"[BB] subscribed f16 {bb['match_id']} ({slug})")
            except Exception as e:
                self.log(f"[BB] f16 failed {bb['match_id']}: {e}")
        return binding

    def live_bc(self, slug):
        with self.lock:
            b = self.bindings.get(slug)
        if not b: return {}
        stakes = self.snapshot_stakes(b["match_id"])
        if not stakes: return {}
        if b["kind"] == "map_winner":
            winners = _extract_map_winner(stakes, b["arg"])
            if not winners: return {}
            out = {}
            for pm_oc, side in b["map"].items():
                st = winners.get(side)
                if not st: continue
                f = st.get("factor")
                if f is None or f <= 1.0: continue
                out[pm_oc] = {"src": "BetBoom", "o": round(float(f), 4), "c": 0.0}
            return out
        if b["kind"] == "winner":
            winners = _extract_winner(stakes, [])
            if not winners: return {}
            out = {}
            for pm_oc, side in b["map"].items():
                st = winners.get(side)
                if not st: continue
                f = st.get("factor")
                if f is None or f <= 1.0: continue
                out[pm_oc] = {"src": "BetBoom", "o": round(float(f), 4), "c": 0.0}
            return out
        if b["kind"] == "total":
            totals = _extract_total(stakes, b["arg"])
            if not totals: return {}
            out = {}
            pm_o = list(b["map"].keys())
            if len(pm_o) == 2:
                o_st = totals.get("over")
                u_st = totals.get("under")
                if o_st and o_st.get("factor", 0) > 1.0:
                    out[pm_o[0]] = {"src": "BetBoom", "o": round(float(o_st["factor"]), 4), "c": 0.0}
                if u_st and u_st.get("factor", 0) > 1.0:
                    out[pm_o[1]] = {"src": "BetBoom", "o": round(float(u_st["factor"]), 4), "c": 0.0}
            return out
        return {}

_GLOBAL = None
_GLOBAL_LOCK = threading.Lock()

def get_global_stream(verbose=False):
    global _GLOBAL
    with _GLOBAL_LOCK:
        if _GLOBAL is None:
            _GLOBAL = BBStream(verbose=verbose)
            _GLOBAL.start()
        elif verbose:
            _GLOBAL.verbose = True
        return _GLOBAL

def subscribe_match_full(match_id):
    get_global_stream().subscribe_f16(match_id)

BB_ENABLED = True
_BB_ERR = None

def wait_ready(timeout=120, min_matches=1, poll=0.5):
    s = get_global_stream()
    if s is None: return False
    t0 = time.time()
    last_log = 0
    while time.time() - t0 < timeout:
        try: n = len(s.matches)
        except Exception: n = 0
        if n >= min_matches:
            print(f"    [wait_ready] накопил {n} матчей за {int(time.time()-t0)}с")
            return True
        now = time.time()
        if now - last_log >= 5:
            print(f"    [wait_ready] {n} матчей... (нужно {min_matches})")
            last_log = now
        time.sleep(poll)
    return False

def bind_slug(slug, market):
    s = get_global_stream()
    if s is None: return None
    try: return s.bind(slug, market)
    except Exception: return None

def get_live_bc(slug):
    s = get_global_stream()
    if s is None: return None
    try:
        if slug not in s.bindings: return None
    except Exception: return None
    try: return s.live_bc(slug)
    except Exception: return None

def get_binding(slug):
    s = get_global_stream()
    if s is None: return None
    try:
        with s.lock:
            b = s.bindings.get(slug)
            return dict(b) if b else None
    except Exception:
        return None

def stop_global_stream():
    global _GLOBAL
    with _GLOBAL_LOCK:
        if _GLOBAL is not None:
            _GLOBAL._stop.set()
            _GLOBAL = None
