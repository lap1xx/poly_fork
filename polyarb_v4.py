#!/usr/bin/env python3
# polyarb_v4.py v41 - BC odds auto-filled from BetBoom AND 1win (bk_router.py).
# LIVE: BB primary + 1win fallback. PREMATCH: 1win only.
# v40: фикс dedup-алертов (больше нет спама одинаковых вилок).
# v41: интеграция bk_router (BB + 1win) — см. память проекта.
import argparse
import csv
import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
import webbrowser
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

import requests
if sys.platform == "win32":
    os.system("")

try:
    import websocket
    HAS_WS = True
except ImportError:
    HAS_WS = False

# ---------- БК: bk_router (BB + 1win) с fallback на bb_bridge ----------
ROUTER_ENABLED = False
BB_ENABLED = False
_BB_ERR = None

try:
    from bk_router import get_router, is_prematch_slug
    ROUTER_ENABLED = True
    BB_ENABLED = True  # косвенно, чтобы старая логика тоже работала
except Exception as _router_err:
    _BB_ERR = _router_err
    try:
        from bb_bridge import get_global_stream, bind_slug, get_live_bc, wait_ready
        BB_ENABLED = True
    except Exception as _bb_err2:
        BB_ENABLED = False
        _BB_ERR = _bb_err2


COLON = chr(58)
SLASH = chr(47)
DOT = chr(46)
HT = "https" + COLON + SLASH + SLASH
WSS = "wss" + COLON + SLASH + SLASH
GAMMA = HT + "gamma-api" + DOT + "polymarket" + DOT + "com"
WS_URL = WSS + "ws-subscriptions-clob" + DOT + "polymarket" + DOT + "com" + "/ws/market"
PM_FEE_PCT = 0.04
CBR_URL = HT + "www" + DOT + "cbr" + DOT + "ru" + "/scripts/XML_daily.asp"

TIMEOUT = 15.0
BOOKS = {}
LOCK = threading.Lock()
STOP = threading.Event()

RUB = " RUB"


class C:
    G    = chr(27) + "[92m"
    LG   = chr(27) + "[38;5;46m"
    R    = chr(27) + "[91m"
    Y    = chr(27) + "[93m"
    O    = chr(27) + "[38;5;208m"
    B    = chr(27) + "[94m"
    M    = chr(27) + "[95m"
    CY   = chr(27) + "[96m"
    PU   = chr(27) + "[38;5;135m"
    W    = chr(27) + "[97m"
    DIM  = chr(27) + "[2m"
    BOLD = chr(27) + "[1m"
    RST  = chr(27) + "[0m"


MELODY_PREMATCH = [(523, 180), (659, 180), (784, 180), (1047, 550)]
MELODY_LIVE = [(880, 120), (880, 120), (1047, 400)]

CFG_FILE = "config.json"
DB_FILE = "arbs.db"
CSV_FILE = "arbs.csv"
JSONL_FILE = "arbs.jsonl"
BK_FILE = "bk_profiles.json"

DEFAULT_CFG = {
    "bank": 20000,
    "rate": 0,
    "poll": 1.0,
    "slip": 0.0,
    "live_min_roi_show": -3.0,
}
DEFAULT_BK = {
    "BetBoom": {"comm": 0.0, "min_bet": 50, "update_seconds": 1},
    "Winline": {"comm": 0.0, "min_bet": 100, "update_seconds": 2},
    "PARI":    {"comm": 0.0, "min_bet": 100, "update_seconds": 4},
    "1win":    {"comm": 0.0, "min_bet": 50,  "update_seconds": 2},
}

STREAMER_KEYS = [
    "streamer", "showmatch", "show-match", "star-series", "esforce",
    "betboom", "winline", "parimatch", "boom-cup", "cyber-battle",
    "esports-battle", "streamers-battle", "media-tournament",
    "blitz-cup", "show-cup", "beast-cup", "kick", "logitech",
    "play-connect", "winline-star",
]


def sep(ch="=", n=74):
    return ch * n


def line(s=""):
    print(s)


def load_cfg():
    if not os.path.exists(CFG_FILE):
        with open(CFG_FILE, "w", encoding="utf-8") as f:
            json.dump(DEFAULT_CFG, f, indent=2)
        return dict(DEFAULT_CFG)
    try:
        with open(CFG_FILE, encoding="utf-8") as f:
            c = json.load(f)
        o = dict(DEFAULT_CFG)
        o.update(c)
        return o
    except Exception:
        return dict(DEFAULT_CFG)


def load_bk():
    if not os.path.exists(BK_FILE):
        with open(BK_FILE, "w", encoding="utf-8") as f:
            json.dump(DEFAULT_BK, f, indent=2)
        return dict(DEFAULT_BK)
    try:
        with open(BK_FILE, encoding="utf-8") as f:
            d = json.load(f)
        # merge defaults чтобы 1win появился
        out = dict(DEFAULT_BK)
        out.update(d)
        return out
    except Exception:
        return dict(DEFAULT_BK)


def init_db():
    con = sqlite3.connect(DB_FILE)
    con.execute(
        "CREATE TABLE IF NOT EXISTS arbs ("
        "ts TEXT, slug TEXT, sport TEXT, roi REAL, bank REAL, profit REAL, "
        "src1 TEXT, out1 TEXT, odds1 REAL, stake1 REAL, "
        "src2 TEXT, out2 TEXT, odds2 REAL, stake2 REAL)"
    )
    con.execute(
        "CREATE TABLE IF NOT EXISTS trades ("
        "ts TEXT, slug TEXT, source TEXT, bank_used REAL, profit REAL, note TEXT)"
    )
    con.commit()
    con.close()


def log_arb(slug, sport, roi, bank, profit, bets, stakes):
    try:
        con = sqlite3.connect(DB_FILE)
        b1 = bets[0] if len(bets) > 0 else {}
        b2 = bets[1] if len(bets) > 1 else {}
        s1 = stakes[0] if len(stakes) > 0 else 0.0
        s2 = stakes[1] if len(stakes) > 1 else 0.0
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        row = (
            ts, slug, sport, roi, bank, profit,
            b1.get("source", ""), b1.get("outcome", ""),
            b1.get("o", 0), s1,
            b2.get("source", ""), b2.get("outcome", ""),
            b2.get("o", 0), s2,
        )
        con.execute("INSERT INTO arbs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", row)
        con.commit()
        con.close()
    except Exception:
        pass


def log_csv(slug, sport, roi, profit):
    try:
        newfile = not os.path.exists(CSV_FILE)
        with open(CSV_FILE, "a", encoding="utf-8", newline="") as f:
            w = csv.writer(f)
            if newfile:
                w.writerow(["ts", "slug", "sport", "roi", "profit"])
            w.writerow([
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                slug, sport, round(roi, 4), round(profit, 2),
            ])
    except Exception:
        pass


def log_arb_json(slug, sport, roi, bank, profit, payout_rub, bets, stakes,
                 m, rate, first_seen_ts=0.0):
    try:
        legs = []
        for i, b in enumerate(bets):
            stake_rub = float(stakes[i]) if i < len(stakes) else 0.0
            src = b.get("source", "")
            if src == "Polymarket":
                currency = "USD"
                stake_native = stake_rub / rate if rate > 0 else 0.0
            else:
                currency = "RUB"
                stake_native = stake_rub
            legs.append({
                "book": src,
                "selection": b.get("outcome", ""),
                "odds": round(float(b.get("o", 0)), 4),
                "stake_native": round(stake_native, 2),
                "currency": currency,
                "stake_rub": round(stake_rub, 2),
            })
        rec = {
            "ts": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
            "slug": slug, "sport": sport,
            "market_type": detect_market_type(slug, m.get("q", "")),
            "question": m.get("q", "")[:200],
            "live": bool(is_live_slug(slug) or m.get("live", False)),
            "streamer": bool(is_streamer(slug, m.get("q", ""))),
            "roi": round(roi, 5),
            "bank_rub": round(bank, 2),
            "profit_rub": round(profit, 2),
            "payout_rub": round(payout_rub, 2),
            "rate_usdrub": round(rate, 4),
            "first_seen_ts": round(first_seen_ts, 3),
            "legs": legs,
        }
        with open(JSONL_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass


def log_trade(slug, source, bank_used, profit, note):
    try:
        con = sqlite3.connect(DB_FILE)
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        con.execute("INSERT INTO trades VALUES (?,?,?,?,?,?)",
                    (ts, slug, source, bank_used, profit, note))
        con.commit()
        con.close()
        return True
    except Exception:
        return False


def today_stats():
    try:
        today = datetime.now().strftime("%Y-%m-%d")
        con = sqlite3.connect(DB_FILE)
        cur = con.execute(
            "SELECT COUNT(*), COALESCE(SUM(profit),0) FROM arbs WHERE ts LIKE ?",
            (today + "%",),
        )
        r = cur.fetchone()
        con.close()
        return int(r[0]), float(r[1])
    except Exception:
        return 0, 0.0


def copy_to_clipboard(text):
    if sys.platform == "win32":
        try:
            p = subprocess.Popen(["clip"], stdin=subprocess.PIPE, shell=True)
            p.communicate(input=text.encode("utf-16-le"))
            return True
        except Exception:
            return False
    return False


def beep(live=False):
    mel = MELODY_LIVE if live else MELODY_PREMATCH
    if sys.platform == "win32":
        try:
            import winsound
            for _ in range(2):
                for fr, d in mel:
                    winsound.Beep(fr, d)
                time.sleep(0.15)
            return
        except Exception:
            pass
    for _ in range(8):
        sys.stdout.write(chr(7))
        sys.stdout.flush()
        time.sleep(0.25)


def fmt_ago(sec):
    if sec < 1:
        return "0s"
    m = int(sec // 60)
    s = int(sec - m * 60)
    if m > 0:
        return "%dm %ds" % (m, s)
    return "%ds" % s


def fmt_until(iso):
    if not iso:
        return ""
    try:
        s = iso.replace("Z", "+00:00").replace(" ", "T")
        dt = datetime.fromisoformat(s)
        now = datetime.now(timezone.utc)
        sec = (dt - now).total_seconds()
        if sec < 0:
            return "started"
        m = int(sec // 60)
        if m < 60:
            return "in %dm" % m
        h = int(m // 60)
        return "in %dh %dm" % (h, m - h * 60)
    except Exception:
        return ""


def is_streamer(slug, q):
    s = (slug + " " + q).lower()
    for k in STREAMER_KEYS:
        if k in s:
            return True
    return False


def detect_sport(slug):
    s = slug.lower()
    tbl = [
        ("cs2-", "CS2"), ("dota2-", "DOTA2"),
        ("lol-", "LOL"), ("valorant-", "VALORANT"),
        ("atp-", "TENNIS"), ("wta-", "TENNIS"), ("itf-", "TENNIS"),
    ]
    for p, n in tbl:
        if s.startswith(p):
            return n
    return "OTHER"


def detect_market_type(slug, question):
    s = (slug + " " + question).lower()
    if "handicap" in s or "spread" in s:
        return "HANDICAP"
    if ("total-games" in s or "round-total" in s
            or "match-total" in s or "set-totals" in s):
        return "TOTAL"
    keys = [
        ("-game1", "MAP1"), ("-game2", "MAP2"), ("-game3", "MAP3"),
        ("-game4", "MAP4"), ("-game5", "MAP5"),
        ("-set-2", "SET2"), ("-set-3", "SET3"),
        ("total-games", "TOTAL"), ("round-total", "TOTAL"),
        ("match-total", "TOTAL"), ("set-totals", "TOTAL"),
        ("handicap", "HANDICAP"), ("spread", "HANDICAP"),
        ("-map-handicap", "HANDICAP"), ("-game-handicap", "HANDICAP"),
    ]
    for p, n in keys:
        if p in s:
            return n
    return "MATCH"


def is_live_slug(slug):
    s = slug.lower()
    for k in ["-game1", "-game2", "-game3", "-game4", "-game5",
              "-set-2", "-set-3", "-set-4", "-set-5", "-live", "-inplay"]:
        if k in s:
            return True
    return False


def linked_slugs(slug):
    base = slug
    for k in ["-game1", "-game2", "-game3", "-game4", "-game5",
              "-set-2", "-set-3", "-set-4", "-set-5"]:
        if base.endswith(k):
            base = base[:-len(k)]
            break
    out = []
    for sfx in ["", "-game1", "-game2", "-game3", "-game4",
                "-game5", "-set-2", "-set-3", "-set-4"]:
        c = base + sfx
        if c != slug and c not in out:
            out.append(c)
    return out


def color_sport(sport):
    if sport == "CS2":
        return C.G
    if sport == "DOTA2":
        return C.M
    if sport == "LOL":
        return C.CY
    if sport == "VALORANT":
        return C.R
    if sport == "TENNIS":
        return C.B
    return C.W


def color_roi(roi):
    if roi >= 0.10:
        return C.LG + C.BOLD
    if roi >= 0.05:
        return C.G + C.BOLD
    if roi >= 0.02:
        return C.Y + C.BOLD
    if roi > 0:
        return C.O + C.BOLD
    if roi >= -1.0:
        return C.Y
    return C.R


def color_mtype(mtype):
    if mtype == "MATCH":
        return C.B
    if mtype.startswith("MAP"):
        return C.PU
    if mtype.startswith("SET"):
        return C.PU
    if mtype == "TOTAL":
        return C.CY
    if mtype == "HANDICAP":
        return C.O
    return C.W


def color_bk_speed(bk_name, bk_profiles):
    sec = bk_profiles.get(bk_name, {}).get("update_seconds", 0)
    if sec == 0:
        return C.W
    if sec <= 2:
        return C.LG
    if sec <= 5:
        return C.Y
    return C.R


class Feed(threading.Thread):
    def __init__(self, ids):
        super().__init__(daemon=True)
        self.ids = ids

    def run(self):
        while not STOP.is_set():
            try:
                ws = websocket.create_connection(WS_URL, timeout=10)
                p = {"type": "market", "assets_ids": self.ids, "initial_dump": True}
                ws.send(json.dumps(p))
                last = time.time()
                while not STOP.is_set():
                    ws.settimeout(1.0)
                    try:
                        m = ws.recv()
                    except Exception:
                        m = None
                    if m and m != "PONG":
                        self._h(m)
                    if time.time() - last > 50:
                        try:
                            ws.send("PING")
                        except Exception:
                            pass
                        last = time.time()
            except Exception:
                time.sleep(2)

    def _h(self, raw):
        try:
            d = json.loads(raw)
        except Exception:
            return
        items = d if isinstance(d, list) else [d]
        for it in items:
            self._a(it)

    def _a(self, d):
        et = d.get("event_type")
        if et == "book":
            aid = d.get("asset_id")
            if not aid:
                return
            bids = []
            asks = []
            for x in d.get("bids", []) or []:
                try:
                    sz = float(x["size"])
                    if sz > 0:
                        bids.append((float(x["price"]), sz))
                except Exception:
                    pass
            for x in d.get("asks", []) or []:
                try:
                    sz = float(x["size"])
                    if sz > 0:
                        asks.append((float(x["price"]), sz))
                except Exception:
                    pass
            bids.sort(key=lambda z: -z[0])
            asks.sort(key=lambda z: z[0])
            now = time.time()
            with LOCK:
                prev = BOOKS.get(aid)
                prev_ask = prev["asks"][0][0] if prev and prev.get("asks") else None
                cur_ask = asks[0][0] if asks else None
                if prev is None or prev_ask != cur_ask:
                    last_change = now
                else:
                    last_change = prev.get("last_change", now)
                BOOKS[aid] = {"bids": bids, "asks": asks,
                              "ts": now, "last_change": last_change}
            return
        if et == "price_change":
            pcs = d.get("price_changes") or d.get("changes") or []
            now = time.time()
            with LOCK:
                for ch in pcs:
                    aid = ch.get("asset_id")
                    if not aid:
                        continue
                    prev = BOOKS.get(aid)
                    if not prev:
                        continue
                    try:
                        side = (ch.get("side") or "").upper()
                        price = float(ch.get("price", 0))
                        size = float(ch.get("size", 0))
                    except Exception:
                        continue
                    if price <= 0:
                        continue
                    bids = {p: s for p, s in prev.get("bids", [])}
                    asks = {p: s for p, s in prev.get("asks", [])}
                    book_side = bids if side == "BUY" else asks
                    if size <= 0:
                        book_side.pop(price, None)
                    else:
                        book_side[price] = size
                    new_bids = sorted(bids.items(), key=lambda z: -z[0])
                    new_asks = sorted(asks.items(), key=lambda z: z[0])
                    prev_ask = prev["asks"][0][0] if prev.get("asks") else None
                    cur_ask = new_asks[0][0] if new_asks else None
                    if prev_ask != cur_ask:
                        last_change = now
                    else:
                        last_change = prev.get("last_change", now)
                    BOOKS[aid] = {"bids": new_bids, "asks": new_asks,
                                  "ts": now, "last_change": last_change}
            return
        if et == "last_trade_price":
            aid = d.get("asset_id")
            if not aid:
                return
            now = time.time()
            with LOCK:
                prev = BOOKS.get(aid)
                if prev:
                    prev["ts"] = now
            return


BOOK_TTL = 30.0


def best_ask(aid):
    with LOCK:
        b = BOOKS.get(aid)
        if not b or not b["asks"]:
            return 0.0
        ts = b.get("ts", 0.0)
        if ts <= 0 or (time.time() - ts) > BOOK_TTL:
            return 0.0
        return b["asks"][0][0]


def walk_book(aid, target):
    with LOCK:
        b = BOOKS.get(aid)
        if not b:
            return None
        ts = b.get("ts", 0.0)
        if ts <= 0 or (time.time() - ts) > BOOK_TTL:
            return None
        levels = list(b["asks"])
    rem = target
    total = 0.0
    for p, s in levels:
        t = min(rem, s)
        total += t * p
        rem -= t
        if rem <= 1e-9:
            break
    if rem > 1e-9:
        return None
    return total / target


def pm_age_sec(aid):
    with LOCK:
        b = BOOKS.get(aid)
        if not b:
            return None
        lc = b.get("last_change") or b.get("ts") or 0
        if lc <= 0:
            return None
        return round(time.time() - lc, 1)


_FRESH_COOLDOWN = {}
_FRESH_MIN_INTERVAL = 20.0
_STALE_THRESHOLD = 15.0


def _fresh_pm_book(token_id, timeout=6.0):
    try:
        r = requests.get("https://clob.polymarket.com/book",
                         params={"token_id": token_id}, timeout=timeout)
        r.raise_for_status()
        book = r.json()
    except Exception:
        return None
    bids, asks = [], []
    for x in (book.get("bids") or []):
        try:
            p = float(x.get("price", 0)); s = float(x.get("size", 0))
            if p > 0 and s > 0:
                bids.append((p, s))
        except Exception:
            pass
    for x in (book.get("asks") or []):
        try:
            p = float(x.get("price", 0)); s = float(x.get("size", 0))
            if p > 0 and s > 0:
                asks.append((p, s))
        except Exception:
            pass
    if not bids and not asks:
        return None
    bids.sort(key=lambda z: -z[0])
    asks.sort(key=lambda z: z[0])
    return bids, asks


def _maybe_refresh_stale(token_id):
    age = pm_age_sec(token_id)
    if age is None or age < _STALE_THRESHOLD:
        return False
    now = time.time()
    last = _FRESH_COOLDOWN.get(token_id, 0.0)
    if now - last < _FRESH_MIN_INTERVAL:
        return False
    _FRESH_COOLDOWN[token_id] = now
    fresh = _fresh_pm_book(token_id)
    if not fresh:
        return False
    bids_f, asks_f = fresh
    with LOCK:
        prev = BOOKS.get(token_id)
        prev_ask = prev["asks"][0][0] if prev and prev.get("asks") else None
        cur_ask = asks_f[0][0] if asks_f else None
        if prev is None or prev_ask != cur_ask:
            last_change = now
        else:
            last_change = prev.get("last_change", now) if prev else now
        BOOKS[token_id] = {"bids": bids_f, "asks": asks_f,
                           "ts": now, "last_change": last_change}
    return True


def get_rate():
    try:
        r = requests.get(CBR_URL, timeout=10)
        r.raise_for_status()
        root = ET.fromstring(r.content)
        for v in root.findall("Valute"):
            c = v.find("CharCode")
            if c is not None and c.text == "USD":
                val = v.find("Value")
                nom = v.find("Nominal")
                if val is not None and nom is not None:
                    return float(val.text.replace(",", ".")) / float(nom.text)
    except Exception:
        return None
    return None


def eff_mult(odds, comm, slip):
    if odds <= 1.0:
        return 0.0
    x = odds * (1.0 - slip)
    if x <= 1.0:
        return 0.0
    return 1.0 + (x - 1.0) * (1.0 - comm)


def compute(bets, bank):
    ms = [eff_mult(b["o"], b["c"], b["s"]) for b in bets]
    if any(m <= 0 for m in ms):
        return None
    inv = sum(1.0 / m for m in ms)
    roi = 1.0 / inv - 1.0
    stakes = [bank * (1.0 / m) / inv for m in ms]
    return {"roi": roi, "stakes": stakes, "payout": bank / inv}


def refine_stakes(bets, result, rate):
    if not result:
        return None
    for _ in range(3):
        changed = False
        for i, b in enumerate(bets):
            if b.get("source") != "Polymarket":
                continue
            stake_rub = result["stakes"][i]
            if stake_rub <= 0:
                continue
            ba = best_ask(b.get("token", ""))
            if ba <= 0:
                continue
            shares = stake_rub / (ba * rate)
            avg = walk_book(b.get("token", ""), shares)
            if not avg or avg <= 0:
                continue
            new_o = (1.0 / (avg * rate)) / (1.0 + PM_FEE_PCT)
            if abs(new_o - b["o"]) / b["o"] > 0.0005:
                b["o"] = new_o
                changed = True
        if not changed:
            break
        result = compute(bets, result["payout"] / (1 + result["roi"]))
    return result


def fetch_market(slug):
    try:
        r = requests.get(GAMMA + "/markets", params={"slug": slug}, timeout=TIMEOUT)
        r.raise_for_status()
        d = r.json()
    except Exception:
        return None
    if not d:
        return None
    m = d[0]
    try:
        start = (m.get("eventStartTime") or m.get("gameStartTime") or "")
        return {
            "slug": m["slug"],
            "q": m.get("question", ""),
            "o": json.loads(m["outcomes"]),
            "t": json.loads(m["clobTokenIds"]),
            "start": start,
            "live": m.get("live", False) or _is_live_by_time(start),
        }
    except Exception:
        return None


def _is_live_by_time(iso):
    if not iso:
        return False
    try:
        s = iso.replace("Z", "+00:00").replace(" ", "T")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt <= datetime.now(timezone.utc)
    except Exception:
        return False


def fetch_live_pm_slugs():
    tags = ["esports", "cs2", "dota-2", "league-of-legends",
            "valorant", "tennis", "atp"]
    out = set()
    for tg in tags:
        try:
            r = requests.get(
                GAMMA + "/events",
                params={"tag_slug": tg, "limit": 200,
                        "active": "true", "closed": "false"},
                timeout=TIMEOUT,
            )
            r.raise_for_status()
            events = r.json()
        except Exception:
            continue
        for ev in events:
            for m in (ev.get("markets") or []):
                if m.get("closed") is True:
                    continue
                if m.get("acceptingOrders") is False:
                    continue
                s = m.get("slug")
                if s:
                    out.add(s)
    return list(out)


def fetch_live_pm_markets():
    tags = ["esports", "cs2", "dota-2", "league-of-legends",
            "valorant", "tennis", "atp"]
    PREFIXES = ("cs2-", "dota2-", "lol-", "valorant-",
                "atp-", "wta-", "itf-")
    out = {}
    for tg in tags:
        for live_mode in ("true", None):
            try:
                params = {"tag_slug": tg, "limit": 200,
                          "active": "true", "closed": "false"}
                if live_mode:
                    params["live"] = live_mode
                r = requests.get(GAMMA + "/events", params=params, timeout=TIMEOUT)
                r.raise_for_status()
                events = r.json()
            except Exception:
                continue
            for ev in events:
                for mk in (ev.get("markets") or []):
                    sl = mk.get("slug", "")
                    if not sl or not sl.startswith(PREFIXES):
                        continue
                    if mk.get("closed") is True:
                        continue
                    if mk.get("acceptingOrders") is False:
                        continue
                    try:
                        o = json.loads(mk.get("outcomes", "[]"))
                        t = json.loads(mk.get("clobTokenIds", "[]"))
                    except Exception:
                        continue
                    if len(o) != 2 or len(t) != 2:
                        continue
                    start = (mk.get("eventStartTime") or ev.get("startTime") or "")
                    out[sl] = {
                        "slug": sl, "q": mk.get("question", ""),
                        "o": o, "t": t, "start": start,
                        "live": bool(mk.get("live", False) or _is_live_by_time(start)),
                    }
    return out


def print_arb(m, bets, r, rate, first_seen, bk):
    sport = detect_sport(m["slug"])
    sc = color_sport(sport)
    mtype = detect_market_type(m["slug"], m.get("q", ""))
    mtc = color_mtype(mtype)
    is_live = is_live_slug(m["slug"]) or m.get("live", False)
    live_tag = (C.Y + C.BOLD + " [LIVE]" + C.RST) if is_live \
               else (C.B + " [PREMATCH]" + C.RST)
    streamer_tag = (C.M + " [STREAMER]" + C.RST) \
                   if is_streamer(m["slug"], m.get("q", "")) else ""

    print()
    print(C.BOLD + sep("=", 74) + C.RST)
    head = C.BOLD + C.G + "  [+] ARB FOUND" + C.RST + "  " \
        + sc + "[" + sport + "]" + C.RST + live_tag + " " \
        + mtc + "[" + mtype + "]" + C.RST + streamer_tag
    print(head)
    print(C.BOLD + sep("=", 74) + C.RST)
    print("  " + C.BOLD + m.get("q", "")[:90] + C.RST)
    print("  " + C.DIM + "slug: " + m["slug"] + C.RST)
    print()

    for i in range(len(bets)):
        b = bets[i]
        st = r["stakes"][i]
        src = b["source"]
        speed = bk.get(src, {}).get("update_seconds", 0)
        speed_col = color_bk_speed(src, bk)
        speed_str = (speed_col + "(%ds)" % speed + C.RST) if speed > 0 else ""
        if src == "Polymarket":
            src_col = C.M
        elif src == "1win":
            src_col = C.O
        else:
            src_col = C.G

        if src == "Polymarket" and rate and rate > 0:
            stake_str = "$%.2f" % (st / rate)
            if (st / rate) < 1.0:
                stake_str += " !"
        else:
            stake_str = "%.0f RUB" % st

        row1 = "  %s%-12s%s %-12s %-26s" % (
            src_col, src, C.RST, speed_str, b["outcome"][:26],
        )
        row2 = " k=%s%.3f%s  stake=%s%s%s%s" % (
            C.BOLD, b["o"], C.RST, C.BOLD, C.W, stake_str, C.RST,
        )
        print(row1 + row2)

    print()
    roi_col = color_roi(r["roi"])
    print("  ROI:      %s%+.2f%%%s" % (roi_col, r["roi"] * 100, C.RST))
    profit_b = r["payout"] - (r["payout"] / (1 + r["roi"]))
    print("  Profit:   %s%.0f%s%s" % (C.G, profit_b, RUB, C.RST))
    print("  Payout:   %s%.0f%s%s" % (C.W, r["payout"], RUB, C.RST))
    print("  Type:     " + (C.Y + "LIVE" + C.RST if is_live else C.B + "PREMATCH" + C.RST))


# =================================================================
#          BK <-> Polymarket integration (bk_router + fallback BB)
# =================================================================

def _pm_token(slug):
    m = fetch_market(slug)
    return (m or {}).get("t") or []


def _book_top_levels(token_id, n=3):
    with LOCK:
        b = BOOKS.get(token_id)
        if not b or not b.get("asks"):
            return []
        ts = b.get("ts", 0.0)
        if ts <= 0 or (time.time() - ts) > BOOK_TTL:
            return []
        return list(b["asks"][:n])


def build_arb_candidates(m, live_bc, slip, bank=0.0, rate=0.0, bk_source="BetBoom"):
    pm_o = m.get("o") or []
    pm_t = m.get("t") or []
    if len(pm_o) != 2 or len(pm_t) != 2 or not live_bc:
        return []

    usd_target = 50.0
    try:
        if bank and rate and rate > 0:
            usd_target = max(20.0, min(200.0, bank / rate * 0.5))
    except Exception:
        pass

    pm_legs = []
    for i, oc in enumerate(pm_o):
        try:
            _maybe_refresh_stale(pm_t[i])
        except Exception:
            pass
        ba = best_ask(pm_t[i])
        if ba <= 0:
            return []
        shares = usd_target / ba
        avg = walk_book(pm_t[i], shares)
        if not avg or avg <= 0:
            return []
        if avg > ba * 1.05:
            return []
        try:
            ba_levels = _book_top_levels(pm_t[i], n=3)
            total_top = sum(p * s for p, s in ba_levels)
            if total_top < usd_target * 0.8:
                return []
        except Exception:
            pass
        pm_legs.append({
            "source": "Polymarket",
            "outcome": oc,
            "o": (1.0 / avg) / (1.0 + PM_FEE_PCT),
            "c": 0.0,
            "s": slip,
            "token": pm_t[i],
            "pm_age": pm_age_sec(pm_t[i]),
            "pm_verified": pm_t[i] in _FRESH_COOLDOWN,
        })

    bk_legs = {}
    for oc, info in live_bc.items():
        try:
            o = float(info.get("o") or 0)
        except Exception:
            continue
        if o <= 1.0:
            continue
        bk_legs[oc] = {
            "source": bk_source,
            "outcome": oc,
            "o": o,
            "c": float(info.get("c", 0.0)),
            "s": slip,
        }

    pairs = []
    for i, pm_oc in enumerate(pm_o):
        other = pm_o[1 - i]
        bb = bk_legs.get(other)
        if bb:
            pairs.append([pm_legs[i], bb])
    return pairs


# =================================================================
#                              main
# =================================================================

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--slugs", default="", help="comma-separated PM slugs")
    ap.add_argument("--live", action="store_true", help="force LIVE tag")
    ap.add_argument("--poll", type=float, default=0.3, help="seconds between checks")
    ap.add_argument("--bank", type=float, default=0.0, help="override bank size (RUB)")
    ap.add_argument("--min-roi", type=float, default=0.3,
                    help="show arbs with roi >= this %% (default 0.3)")
    ap.add_argument("--no-bb", action="store_true", help="disable ALL BK bridge")
    ap.add_argument("--watch", action="store_true", help="auto-refresh PM live markets")
    ap.add_argument("--watch-sec", type=int, default=60, help="watch refresh interval")
    ap.add_argument("--alert-gap", type=int, default=120,
                    help="seconds between repeat alerts for same slug")
    args = ap.parse_args()

    cfg = load_cfg()
    bk  = load_bk()
    init_db()

    bank    = args.bank if args.bank > 0 else float(cfg.get("bank", 20000))
    slip    = float(cfg.get("slip", 0.0))
    poll    = args.poll if args.poll > 0 else float(cfg.get("poll", 1.0))
    min_roi = args.min_roi / 100.0

    slugs = [x.strip() for x in args.slugs.split(",") if x.strip()]
    if not slugs and not args.watch:
        print("[!] no --slugs given (use --watch for auto)")
        return 1

    market_map = {}
    if args.watch:
        print("[*] watch mode: fetching PM live markets...")
        market_map = fetch_live_pm_markets()
        print("[*] got %d real match markets" % len(market_map))
    else:
        print("[*] resolving %d PM markets..." % len(slugs))
        for s in slugs:
            m = fetch_market(s)
            if m:
                market_map[s] = m
            else:
                print("    [!] not found: " + s)
        print("[*] resolved %d / %d" % (len(market_map), len(slugs)))
    if not market_map and not args.watch:
        return 1

    # ---------- BK bridge: bk_router (BB + 1win) с fallback на BB-only ----------
    router = None
    if ROUTER_ENABLED and not args.no_bb:
        print("[*] starting bk_router (BB + 1win)...")
        try:
            router = get_router(use_bb=True, use_onewin=True)
            router.start()
            ok_bb = router.wait_bb(timeout=60)
            ok_ow = router.wait_onewin(timeout=45, min_matches=10)
            print("[*] BB ready: %s | 1win ready: %s" % (ok_bb, ok_ow))
            if not ok_bb and not ok_ow:
                print("[!] ни один БК не готов — выход")
                return 1
            st = router.stats()
            print("[*] BK stats: %s" % st)
        except Exception as e:
            print("[!] bk_router init failed: %s" % e)
            router = None

    if router is None:
        if not (BB_ENABLED and not args.no_bb):
            why = _BB_ERR if not BB_ENABLED else "disabled"
            print("[!] нет активных БК: %s" % why)
            return 1
        print("[*] fallback: BB-only")
        try:
            get_global_stream(verbose=False)
            wait_ready(timeout=60)
        except Exception as e:
            print("[!] BB start failed: %s" % e)
            return 1

    # Первичный bind
    bound_bb = bound_ow = 0
    for slug, m in market_map.items():
        try:
            if router is not None:
                b, src = router.bind(slug, m)
                if b:
                    if src == "BetBoom":
                        bound_bb += 1
                    else:
                        bound_ow += 1
            else:
                if bind_slug(slug, m):
                    bound_bb += 1
        except Exception as e:
            print("    [!] bind %s: %s" % (slug, e))
    print("[*] bound BB=%d 1win=%d / %d" % (bound_bb, bound_ow, len(market_map)))

    # ---------- CBR rate ----------
    rate = get_rate()
    if rate:
        print("[*] USD/RUB = %.3f" % rate)
    else:
        rate = 1.0
        print("[!] CBR unavailable, rate=1")

    # ---------- PM feed ----------
    tokens = sorted({t for m in market_map.values() for t in (m.get("t") or [])})
    if not tokens:
        print("[!] no tokens")
        return 1
    print("[*] starting PM book feed (%d tokens)..." % len(tokens))
    feed = Feed(tokens)
    feed.start()
    time.sleep(2.0)

    print("[*] main loop (Ctrl+C to stop), min_roi=%.2f%%" % (min_roi * 100))
    print()

    first_seen = {}
    last_alert = {}
    last_roi = {}
    last_sig = {}
    printed_once = set()
    upd_count = {}
    alert_gap = float(args.alert_gap)

    def retry_bind():
        if router is not None:
            with router._lock:
                src_now = set(router.bindings.keys())
            unbound = [s for s in market_map if s not in src_now]
        else:
            stream = get_global_stream()
            unbound = [s for s in market_map if s not in stream.bindings]
        if not unbound:
            return 0
        n = 0
        for slug in unbound:
            try:
                if router is not None:
                    b, _ = router.bind(slug, market_map[slug])
                    if b:
                        n += 1
                else:
                    if bind_slug(slug, market_map[slug]):
                        n += 1
            except Exception:
                pass
        if n:
            print("[*] retry-bind: +%d (unbound было %d)" % (n, len(unbound)))
        return n

    last_retry = time.time()
    last_pm_refresh = 0.0

    try:
        while not STOP.is_set():
            if args.watch and time.time() - last_pm_refresh >= args.watch_sec:
                fresh = fetch_live_pm_markets()
                added = 0
                for s, mm in fresh.items():
                    if s not in market_map:
                        added += 1
                    market_map[s] = mm
                if added:
                    print("[*] watch: +%d new markets (total %d)"
                          % (added, len(market_map)))
                last_pm_refresh = time.time()

            if time.time() - last_retry >= 10:
                retry_bind()
                last_retry = time.time()

            live_snap = {}
            for slug, m in market_map.items():
                if router is not None:
                    live_bc = router.live_bc(slug)
                    _bk_src = router.get_src(slug) or "BetBoom"
                else:
                    live_bc = get_live_bc(slug)
                    _bk_src = "BetBoom"
                if not live_bc:
                    continue

                pairs = build_arb_candidates(m, live_bc, slip, bank, rate,
                                             bk_source=_bk_src)
                for bets in pairs:
                    r = compute(bets, bank)
                    if not r:
                        continue
                    roi = r["roi"]
                    if roi < min_roi:
                        continue

                    # --- web snapshot ---
                    try:
                        _prf = r["payout"] - (r["payout"] / (1.0 + roi))
                        _bb_b = None
                        _bb_stakes_debug = []
                        if router is not None:
                            try:
                                _pair = router.bindings.get(slug)
                                _bb_b = _pair[1] if _pair else None
                                _bb_stakes_debug = router.bb_stakes_debug(slug)
                            except Exception as _e:
                                _bb_stakes_debug = [{'err': str(_e)}]
                        else:
                            try:
                                from bb_bridge import get_binding as _gb, get_global_stream as _g2
                                _bb_b = _gb(slug)
                                _s2 = _g2()
                                if _bb_b:
                                    for _st in _s2.snapshot_stakes(_bb_b['match_id']):
                                        _mn = (_st.get('market_name') or '').lower()
                                        _pn = (_st.get('period_name') or '').lower()
                                        _want = (
                                            'map' in _mn or 'карт' in _mn
                                            or 'winner' in _mn or 'исход' in _mn
                                            or 'map' in _pn or 'карт' in _pn
                                        )
                                        if not _want:
                                            continue
                                        _bb_stakes_debug.append({
                                            'market_name': _st.get('market_name'),
                                            'period_name': _st.get('period_name'),
                                            'market_slug': _st.get('market_slug'),
                                            'display_name': _st.get('display_name'),
                                            'short_name': _st.get('short_name'),
                                            'name': _st.get('name'),
                                            'factor': _st.get('factor'),
                                            'map_num': _st.get('map_num'),
                                            'is_active': _st.get('is_active'),
                                            'url': (_st.get('url') or '')[:140],
                                        })
                            except Exception as _e:
                                _bb_stakes_debug = [{'err': str(_e)}]
                        _pm_age = None
                        for _b in bets:
                            if _b.get("source") == "Polymarket":
                                _pm_age = _b.get("pm_age")
                                break
                        live_snap[slug] = {
                            "slug": slug,
                            "q": m.get("q", ""),
                            "sport": detect_sport(slug),
                            "market_type": detect_market_type(slug, m.get("q", "")),
                            "roi": float(roi),
                            "bank": float(bank),
                            "rate": float(rate or 0),
                            "bets": [
                                {
                                    "source": b.get("source"),
                                    "outcome": b.get("outcome"),
                                    "o": float(b.get("o") or 0),
                                    "c": float(b.get("c") or 0),
                                    "s": float(b.get("s") or 0),
                                    "pm_age": b.get("pm_age"),
                                } for b in bets
                            ],
                            "stakes": [float(x) for x in r["stakes"]],
                            "payout": float(r["payout"]),
                            "profit": float(_prf),
                            "is_live": bool(is_live_slug(slug) or _is_live_by_time(m.get("start", ""))),
                            "is_streamer": bool(is_streamer(slug, m.get("q", ""))),
                            "pm_age": _pm_age,
                            "bk_source": _bk_src,
                            "bb_match_id": (_bb_b or {}).get("match_id"),
                            "bb_tournament_id": (_bb_b or {}).get("tournament_id"),
                            "bb_binding": _bb_b,
                            "bb_stakes_debug": _bb_stakes_debug,
                            "ts": time.time(),
                        }
                    except Exception:
                        pass
                    # --- /web snapshot ---

                    now = time.time()
                    prev_sig = last_sig.get(slug)
                    prev_roi = last_roi.get(slug)

                    sig = (round(float(bets[0]["o"]), 2),
                           round(float(bets[1]["o"]), 2) if len(bets) > 1 else None)

                    changed = (prev_sig is None
                               or sig != prev_sig
                               or (prev_roi is not None and abs(roi - prev_roi) >= 0.005))
                    if not changed:
                        continue

                    last_sig[slug] = sig
                    last_alert[slug] = now
                    last_roi[slug] = roi
                    fs = first_seen.setdefault(slug, now)
                    is_live = args.live or is_live_slug(slug) or m.get("live", False)

                    if slug not in printed_once:
                        printed_once.add(slug)
                        upd_count[slug] = 0
                        print_arb(m, bets, r, rate, fs, bk)
                    else:
                        upd_count[slug] = upd_count.get(slug, 0) + 1
                        pm_o = next((b["o"] for b in bets
                                     if b.get("source") == "Polymarket"), 0.0)
                        bk_o = next((b["o"] for b in bets
                                     if b.get("source") != "Polymarket"), 0.0)
                        pm_st = bk_st = 0.0
                        for i, b in enumerate(bets):
                            if b.get("source") == "Polymarket":
                                pm_st = r["stakes"][i] / rate if rate and rate > 0 else r["stakes"][i]
                            else:
                                bk_st = r["stakes"][i]
                        profit_upd = r["payout"] - (r["payout"] / (1.0 + roi))
                        print("  [~] %s [%s] upd#%-3d  PM %.2f/$%.2f  %s %.2f/%.0fR  ROI %+.1f%% (was %+.1f%%)  profit +%.0fR" % (
                            slug[:38],
                            detect_market_type(slug, m.get("q", "")),
                            upd_count[slug],
                            pm_o, pm_st,
                            _bk_src, bk_o, bk_st,
                            roi * 100, prev_roi * 100 if prev_roi is not None else roi * 100,
                            profit_upd,
                        ))

                    profit = r["payout"] - (r["payout"] / (1.0 + roi))
                    try:
                        log_arb(slug, detect_sport(slug), roi, bank, profit,
                                bets, r["stakes"])
                        log_csv(slug, detect_sport(slug), roi, profit)
                        log_arb_json(slug, detect_sport(slug), roi, bank,
                                     profit, r["payout"], bets, r["stakes"],
                                     m, rate, first_seen_ts=fs)
                    except Exception:
                        pass

            _now = time.time()
            if _now - globals().get('_last_snap_write', 0) >= 2.0:
                try:
                    with open("arbs_live.json", "w", encoding="utf-8") as _f:
                        json.dump(live_snap, _f, ensure_ascii=False)
                    globals()['_last_snap_write'] = _now
                except Exception:
                    pass

            time.sleep(poll)
    except KeyboardInterrupt:
        print()
        print("[*] stopped")
    finally:
        STOP.set()
        try:
            if router is not None:
                router.stop()
            else:
                from bb_bridge import stop_global_stream
                stop_global_stream()
        except Exception:
            pass

    return 0


if __name__ == "__main__":
    try:
        sys.exit(main() or 0)
    except KeyboardInterrupt:
        print()
        print("interrupted")