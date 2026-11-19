#!/usr/bin/env python3
# polyarb_v4.py - Polymarket/1win monitor; BetBoom remains an optional fallback.
# The scanner starts the read-only 1win odds adapter as its primary bookmaker source.
# v40: фикс dedup-алертов (больше нет спама одинаковых вилок).
# v41: интеграция bk_router (BB + 1win) — см. память проекта.
import argparse
import csv
from concurrent.futures import ThreadPoolExecutor
import json
import math
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
from event_phase import classify_event_phase
if sys.platform == "win32":
    os.system("")

try:
    import websocket
    HAS_WS = True
except ImportError:
    HAS_WS = False

# ---------- БК: 1win primary, optional BetBoom fallback ----------
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
STATUS_FILE = "scanner_status.json"
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
            "phase": classify_event_phase(m),
            "live": classify_event_phase(m) == "live",
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
    """Legacy label helper; market fragments like map/game/set do not imply LIVE."""
    s = str(slug or "").lower()
    return s.endswith(("-live", "-inplay"))


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
        self.ids = sorted({str(x) for x in ids if x})
        self._ids_lock = threading.RLock()
        self._generation = 0
        self._wake = threading.Event()

    def update_tokens(self, ids):
        """Replace the subscription set; the reader reconnects with a full snapshot."""
        new_ids = sorted({str(x) for x in ids if x})
        with self._ids_lock:
            if new_ids == self.ids:
                return False
            self.ids = new_ids
            self._generation += 1
            self._wake.set()
        return True

    def run(self):
        if not HAS_WS:
            return
        while not STOP.is_set():
            ws = None
            try:
                with self._ids_lock:
                    ids = list(self.ids)
                    generation = self._generation
                if not ids:
                    STOP.wait(1.0)
                    continue
                ws = websocket.create_connection(WS_URL, timeout=10)
                with self._ids_lock:
                    changed_while_connecting = generation != self._generation
                if changed_while_connecting:
                    continue
                p = {"type": "market", "assets_ids": ids, "initial_dump": True}
                ws.send(json.dumps(p))
                last = time.time()
                while not STOP.is_set():
                    with self._ids_lock:
                        if generation != self._generation:
                            break
                    ws.settimeout(1.0)
                    try:
                        m = ws.recv()
                    except websocket.WebSocketTimeoutException:
                        m = None
                    except Exception:
                        break
                    if m and m != "PONG":
                        self._h(m)
                    if time.time() - last > 50:
                        try:
                            ws.send("PING")
                        except Exception:
                            break
                        last = time.time()
            except Exception:
                pass
            finally:
                if ws is not None:
                    try:
                        ws.close()
                    except Exception:
                        pass
            if not STOP.is_set():
                self._wake.wait(2.0)
                self._wake.clear()

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
                    price, sz = float(x["price"]), float(x["size"])
                    if math.isfinite(price) and math.isfinite(sz) and 0 < price < 1 and sz > 0:
                        bids.append((price, sz))
                except Exception:
                    pass
            for x in d.get("asks", []) or []:
                try:
                    price, sz = float(x["price"]), float(x["size"])
                    if math.isfinite(price) and math.isfinite(sz) and 0 < price < 1 and sz > 0:
                        asks.append((price, sz))
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
                    if (not math.isfinite(price) or not math.isfinite(size)
                            or not (0 < price < 1)
                            or side not in {"BUY", "SELL"}):
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


def best_bid(aid):
    with LOCK:
        b = BOOKS.get(aid)
        if not b or not b.get("bids"):
            return 0.0
        ts = b.get("ts", 0.0)
        if ts <= 0 or (time.time() - ts) > BOOK_TTL:
            return 0.0
        return b["bids"][0][0]


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


_PM_REFRESH_EXECUTOR = ThreadPoolExecutor(max_workers=4)
_PM_REFRESH_LOCK = threading.RLock()
_PM_REFRESH_INFLIGHT = set()
_PM_REFRESH_COOLDOWN = {}
_PM_REFRESH_DIAGNOSTICS = {}
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
    if not isinstance(book, dict):
        return None
    bids, asks = [], []
    for x in (book.get("bids") or []):
        try:
            p = float(x.get("price", 0)); s = float(x.get("size", 0))
            if math.isfinite(p) and math.isfinite(s) and 0 < p < 1 and s > 0:
                bids.append((p, s))
        except Exception:
            pass
    for x in (book.get("asks") or []):
        try:
            p = float(x.get("price", 0)); s = float(x.get("size", 0))
            if math.isfinite(p) and math.isfinite(s) and 0 < p < 1 and s > 0:
                asks.append((p, s))
        except Exception:
            pass
    bids.sort(key=lambda z: -z[0])
    asks.sort(key=lambda z: z[0])
    return bids, asks


def pm_book_diagnostic(token_id, now=None):
    """Return cache state without treating an empty or stale book as executable."""
    key = str(token_id)
    now = time.time() if now is None else float(now)
    with LOCK:
        book = BOOKS.get(key) or BOOKS.get(token_id)
        if not book:
            state = "missing_book"
            age = None
            bids, asks = [], []
        else:
            bids = list(book.get("bids") or [])
            asks = list(book.get("asks") or [])
            try:
                ts = float(book.get("ts") or 0.0)
            except (TypeError, ValueError):
                ts = 0.0
            age = max(0.0, now - ts) if ts > 0 else None
            if ts <= 0:
                state = "missing_timestamp"
            elif age > BOOK_TTL:
                state = "stale_book"
            elif not bids and not asks:
                state = "empty_book"
            elif not asks:
                state = "empty_asks"
            else:
                state = "ask_available"
    with _PM_REFRESH_LOCK:
        refresh = dict(_PM_REFRESH_DIAGNOSTICS.get(key) or {})
    return {
        "book_state": state,
        "book_age_sec": round(age, 1) if age is not None else None,
        "bid_levels": len(bids),
        "ask_levels": len(asks),
        "best_bid": bids[0][0] if bids else None,
        "best_ask": asks[0][0] if asks else None,
        "rest_refresh": refresh.get("state", "not_requested"),
        "rest_attempted_at": refresh.get("attempted_at"),
        "rest_completed_at": refresh.get("completed_at"),
    }


def _refresh_pm_book_worker(token_id, attempted_at, reason):
    key = str(token_id)
    result_state = "fetch_failed"
    bid_levels = ask_levels = 0
    try:
        fresh = _fresh_pm_book(key, timeout=4.0)
        if fresh is not None:
            bids, asks = fresh
            bid_levels, ask_levels = len(bids), len(asks)
            now = time.time()
            with LOCK:
                previous = BOOKS.get(key)
                try:
                    previous_ts = float((previous or {}).get("ts") or 0.0)
                except (TypeError, ValueError):
                    previous_ts = 0.0
                if previous_ts > attempted_at:
                    result_state = "websocket_newer"
                else:
                    previous_ask = ((previous or {}).get("asks") or [None])[0]
                    current_ask = asks[0] if asks else None
                    previous_ask_price = previous_ask[0] if previous_ask else None
                    current_ask_price = current_ask[0] if current_ask else None
                    if previous is None or previous_ask_price != current_ask_price:
                        last_change = now
                    else:
                        last_change = previous.get("last_change", now)
                    BOOKS[key] = {
                        "bids": bids, "asks": asks, "ts": now,
                        "last_change": last_change,
                    }
                    if not bids and not asks:
                        result_state = "empty_book"
                    elif not asks:
                        result_state = "empty_asks"
                    else:
                        result_state = "asks_available"
    except Exception:
        result_state = "fetch_failed"
    finally:
        with _PM_REFRESH_LOCK:
            _PM_REFRESH_INFLIGHT.discard(key)
            previous = _PM_REFRESH_DIAGNOSTICS.get(key, {})
            _PM_REFRESH_DIAGNOSTICS[key] = {
                "state": result_state,
                "reason": reason,
                "attempted_at": previous.get("attempted_at", attempted_at),
                "completed_at": time.time(),
                "bid_levels": bid_levels,
                "ask_levels": ask_levels,
            }


def _schedule_pm_book_refresh(token_id, reason):
    key = str(token_id)
    now = time.time()
    with _PM_REFRESH_LOCK:
        last = _PM_REFRESH_COOLDOWN.get(key, 0.0)
        if key in _PM_REFRESH_INFLIGHT or now - last < _FRESH_MIN_INTERVAL:
            return False
        _PM_REFRESH_COOLDOWN[key] = now
        _PM_REFRESH_INFLIGHT.add(key)
        _PM_REFRESH_DIAGNOSTICS[key] = {
            "state": "pending", "reason": reason,
            "attempted_at": now, "completed_at": None,
            "bid_levels": 0, "ask_levels": 0,
        }
    try:
        _PM_REFRESH_EXECUTOR.submit(_refresh_pm_book_worker, key, now, reason)
    except Exception:
        with _PM_REFRESH_LOCK:
            _PM_REFRESH_INFLIGHT.discard(key)
            _PM_REFRESH_DIAGNOSTICS[key] = {
                "state": "schedule_failed", "reason": reason,
                "attempted_at": now, "completed_at": time.time(),
                "bid_levels": 0, "ask_levels": 0,
            }
        return False
    return True


def pm_refresh_attempted(token_id):
    with _PM_REFRESH_LOCK:
        return str(token_id) in _PM_REFRESH_COOLDOWN


def _maybe_refresh_stale(token_id):
    details = pm_book_diagnostic(token_id)
    state = details["book_state"]
    if state in {"missing_book", "missing_timestamp"}:
        return _schedule_pm_book_refresh(token_id, state)
    if details["book_age_sec"] is None or details["book_age_sec"] < _STALE_THRESHOLD:
        return False
    return _schedule_pm_book_refresh(token_id, "stale_book")


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


def resolve_usd_rub_rate(cbr_rate, configured_rate):
    """Prefer fresh CBR data; fall back only to a positive finite configured rate."""
    for candidate in (cbr_rate, configured_rate):
        try:
            rate = float(candidate)
        except (TypeError, ValueError):
            continue
        if math.isfinite(rate) and 0 < rate <= 100_000:
            return rate
    return None


def eff_mult(odds, comm, slip):
    try:
        odds, comm, slip = float(odds), float(comm), float(slip)
    except (TypeError, ValueError):
        return 0.0
    if not all(math.isfinite(x) for x in (odds, comm, slip)):
        return 0.0
    if odds <= 1.0 or not (0.0 <= comm < 1.0) or not (0.0 <= slip < 1.0):
        return 0.0
    x = odds * (1.0 - slip)
    if x <= 1.0:
        return 0.0
    return 1.0 + (x - 1.0) * (1.0 - comm)


def compute(bets, bank):
    try:
        bank = float(bank)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(bank) or bank <= 0 or not bets:
        return None
    try:
        ms = [eff_mult(b["o"], b.get("c", 0.0), b.get("s", 0.0)) for b in bets]
    except (KeyError, TypeError, AttributeError):
        return None
    if any(m <= 0 for m in ms):
        return None
    inv = sum(1.0 / m for m in ms)
    if not math.isfinite(inv) or inv <= 0:
        return None
    roi = 1.0 / inv - 1.0
    stakes = [bank * (1.0 / m) / inv for m in ms]
    payout = bank / inv
    return {"roi": roi, "stakes": stakes, "payout": payout,
            "profit": payout - bank, "bank": bank}


def alert_due(changed, last_alert_ts, now, alert_gap):
    """Allow changed signals after the minimum gap and repeat reminders on schedule."""
    gap = max(0.0, float(alert_gap))
    if changed:
        return last_alert_ts is None or gap == 0.0 or now - last_alert_ts >= gap
    return last_alert_ts is not None and gap > 0.0 and now - last_alert_ts >= gap


def refine_stakes(bets, result, rate):
    if not result or not rate or rate <= 0:
        return None
    bank = sum(float(x) for x in result.get("stakes", []))
    if not math.isfinite(bank) or bank <= 0:
        return None
    for _ in range(5):
        changed = False
        for i, b in enumerate(bets):
            if b.get("source") != "Polymarket":
                continue
            stake_rub = result["stakes"][i]
            if stake_rub <= 0:
                continue
            ba = best_ask(b.get("token", ""))
            if ba <= 0:
                return None
            shares = stake_rub / (ba * rate)
            avg = walk_book(b.get("token", ""), shares)
            if not avg or avg <= 0:
                return None
            # A RUB stake buys stake_rub/(price_usd*rate) shares, then pays
            # one USD per winning share. Converting the payout back to RUB
            # cancels `rate`; the multiplier is 1/avg_price, not 1/(avg*rate).
            new_o = (1.0 / avg) / (1.0 + PM_FEE_PCT)
            if abs(new_o - b["o"]) / b["o"] > 0.0005:
                b["o"] = new_o
                changed = True
        if not changed:
            break
        result = compute(bets, bank)
        if result is None:
            return None
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
        phase = classify_event_phase(m)
        return {
            "slug": m["slug"],
            "q": m.get("question", ""),
            "o": json.loads(m["outcomes"]),
            "t": json.loads(m["clobTokenIds"]),
            "start": start,
            "phase": phase,
            "_phase_classified": True,
            "live": phase == "live",
            "status": m.get("status"),
            "closed": m.get("closed"),
            "active": m.get("active"),
        }
    except Exception:
        return None


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


def fetch_live_pm_markets(with_status=False):
    tags = ["esports", "cs2", "dota-2", "league-of-legends",
            "valorant", "tennis", "atp"]
    PREFIXES = ("cs2-", "dota2-", "lol-", "valorant-",
                "atp-", "wta-", "itf-")
    query_limit = 200
    out = {}
    query_count = 0
    successful_queries = 0
    catalog_complete = True
    for tg in tags:
        for live_mode in ("true", None):
            query_count += 1
            try:
                params = {"tag_slug": tg, "limit": query_limit,
                          "active": "true", "closed": "false"}
                if live_mode:
                    params["live"] = live_mode
                r = requests.get(GAMMA + "/events", params=params, timeout=TIMEOUT)
                r.raise_for_status()
                events = r.json()
                if not isinstance(events, list):
                    catalog_complete = False
                    continue
                successful_queries += 1
            except Exception:
                continue
            if len(events) >= query_limit:
                # No pagination is implemented; a full page may be truncated.
                catalog_complete = False
            for ev in events:
                if not isinstance(ev, dict):
                    catalog_complete = False
                    continue
                event_markets = ev.get("markets") or []
                if not isinstance(event_markets, list):
                    catalog_complete = False
                    continue
                for mk in event_markets:
                    if not isinstance(mk, dict):
                        catalog_complete = False
                        continue
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
                        catalog_complete = False
                        continue
                    if (
                        not isinstance(o, list) or not isinstance(t, list)
                        or len(o) != 2 or len(t) != 2
                        or any(not isinstance(value, str) or not value.strip() for value in o + t)
                    ):
                        catalog_complete = False
                        continue
                    start = (mk.get("eventStartTime") or ev.get("startTime") or "")
                    phase = classify_event_phase(mk, ev)
                    out[sl] = {
                        "slug": sl, "q": mk.get("question", ""),
                        "o": o, "t": t, "start": start,
                        "phase": phase,
                        "_phase_classified": True,
                        "live": phase == "live",
                        "status": mk.get("status") or ev.get("status"),
                        "closed": mk.get("closed", ev.get("closed")),
                        "active": mk.get("active", True),
                    }
    complete = query_count > 0 and successful_queries == query_count and catalog_complete
    return (out, complete) if with_status else out


def reconcile_pm_market_map(current, fresh, refresh_complete):
    """Replace an old Gamma catalog only after a complete successful refresh."""
    current = current if isinstance(current, dict) else {}
    fresh = fresh if isinstance(fresh, dict) else {}
    added = len(set(fresh) - set(current))
    removed = len(set(current) - set(fresh)) if refresh_complete else 0
    merged = dict(fresh) if refresh_complete else {**current, **fresh}
    return merged, added, removed


def print_arb(m, bets, r, rate, first_seen, bk):
    sport = detect_sport(m["slug"])
    sc = color_sport(sport)
    mtype = detect_market_type(m["slug"], m.get("q", ""))
    mtc = color_mtype(mtype)
    phase = classify_event_phase(m)
    phase_label = {"live": "LIVE", "prematch": "PREMATCH", "unknown": "UNKNOWN"}[phase]
    phase_color = C.Y if phase == "live" else C.B if phase == "prematch" else C.DIM
    live_tag = phase_color + C.BOLD + " [" + phase_label + "]" + C.RST
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
    print("  Type:     " + phase_color + phase_label + C.RST)


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


def _write_json_atomic(path, payload):
    """Write a JSON snapshot by replace, so readers never see a partial file."""
    tmp_path = str(path) + ".tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False)
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.remove(tmp_path)
        except Exception:
            pass


def _write_scan_status(payload):
    """Atomically publish read-only scan diagnostics for the dashboard."""
    _write_json_atomic(STATUS_FILE, payload)


def _write_match_diagnostics(router, market_map):
    """Publish explainable 1win matching results without changing arb decisions."""
    if router is None or not getattr(router, "use_onewin", False):
        return
    try:
        from onewin_bridge import get_match_diagnostics
        diagnostics = get_match_diagnostics()
    except Exception:
        return
    active = set(market_map or {})
    diagnostics = {slug: row for slug, row in diagnostics.items() if slug in active}
    reason_counts = {}
    unsupported_subtype_counts = {}
    match_ids = set()
    matched_markets = 0
    for row in diagnostics.values():
        reason = row.get("reason", "unknown")
        reason_counts[reason] = reason_counts.get(reason, 0) + 1
        if reason == "unsupported_market_type":
            subtype = row.get("unsupported_subtype", "unknown")
            unsupported_subtype_counts[subtype] = unsupported_subtype_counts.get(subtype, 0) + 1
        if reason in {"matched", "matched_time_context", "matched_time_corroborated"}:
            matched_markets += 1
            match_id = row.get("selected_match_id")
            if match_id is not None:
                match_ids.add(str(match_id))
    _write_json_atomic("match_diagnostics.json", {
        "ts": time.time(),
        "summary": {
            "markets_with_diagnostics": len(diagnostics),
            "matched_pm_markets": matched_markets,
            "unique_1win_events": len(match_ids),
            "reason_counts": reason_counts,
            "unsupported_subtype_counts": unsupported_subtype_counts,
        },
        "markets": diagnostics,
    })


def build_arb_candidates(m, live_bc, slip, bank=0.0, rate=0.0,
                         bk_source="BetBoom", diagnostics=None,
                         pm_diagnostics=None, market_slug=None):
    def note(reason):
        if diagnostics is not None:
            diagnostics[reason] = diagnostics.get(reason, 0) + 1

    pm_o = m.get("o") or []
    pm_t = m.get("t") or []
    if len(pm_o) != 2 or len(pm_t) != 2 or not live_bc:
        note("invalid_market_or_no_quotes")
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
            note("pm_no_ask")
            if pm_diagnostics is not None:
                row = {
                    "market_slug": str(market_slug or m.get("slug") or ""),
                    "outcome": str(oc),
                    "token_id": str(pm_t[i]),
                }
                row.update(pm_book_diagnostic(pm_t[i]))
                pm_diagnostics.append(row)
            return []
        shares = usd_target / ba
        avg = walk_book(pm_t[i], shares)
        if not avg or avg <= 0:
            note("pm_depth_short")
            return []
        if avg > ba * 1.05:
            note("pm_impact_rejected")
            return []
        pm_legs.append({
            "source": "Polymarket",
            "outcome": oc,
            "o": (1.0 / avg) / (1.0 + PM_FEE_PCT),
            "c": 0.0,
            "s": slip,
            "token": pm_t[i],
            "pm_best_ask": ba,
            "pm_best_bid": best_bid(pm_t[i]),
            "pm_avg_ask": avg,
            "pm_age": pm_age_sec(pm_t[i]),
            "pm_verified": pm_refresh_attempted(pm_t[i]),
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
            "odds_age_sec": info.get("age_sec"),
        }

    pairs = []
    for i, pm_oc in enumerate(pm_o):
        other = pm_o[1 - i]
        bb = bk_legs.get(other)
        if bb:
            pairs.append([pm_legs[i], bb])
    if not pairs:
        note("no_opposite_bk_market")
    return pairs


# =================================================================
#                              main
# =================================================================

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--slugs", default="", help="comma-separated PM slugs")
    ap.add_argument("--live", action="store_true",
                    help="deprecated legacy flag; Gamma metadata determines event phase")
    ap.add_argument("--poll", type=float, default=0.3, help="seconds between checks")
    ap.add_argument("--bank", type=float, default=0.0, help="override bank size (RUB)")
    ap.add_argument("--min-roi", type=float, default=0.3,
                    help="show arbs with roi >= this %% (default 0.3)")
    ap.add_argument("--no-bb", action="store_true", help="disable BetBoom fallback; keep 1win")
    ap.add_argument("--watch", action="store_true", help="auto-refresh PM live markets")
    ap.add_argument("--watch-sec", type=int, default=60, help="watch refresh interval")
    ap.add_argument("--alert-gap", type=int, default=120,
                    help="minimum gap between signal updates and repeat reminders")
    args = ap.parse_args()
    if args.live:
        print("[!] --live is deprecated and ignored; event phase requires source evidence")

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
    pm_catalog_complete = True
    pm_catalog_retained = False
    if args.watch:
        print("[*] watch mode: fetching PM live markets...")
        market_map, pm_catalog_complete = fetch_live_pm_markets(with_status=True)
        print("[*] got %d real match markets" % len(market_map))
        if not pm_catalog_complete:
            print("[!] initial PM catalog refresh was partial; missing query batches will be retried")
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

    # ---------- BK bridge: 1win primary; BetBoom is a fallback ----------
    router = None
    if ROUTER_ENABLED:
        print("[*] starting bookmaker router (1win primary; BetBoom fallback %s)..."
              % ("disabled" if args.no_bb else "enabled"))
        try:
            router = get_router(use_bb=not args.no_bb, use_onewin=True)
            router.start()
            ok_ow = router.wait_onewin(timeout=60, min_matches=1)
            ok_bb = False
            if not ok_ow and router.use_bb:
                ok_bb = router.wait_bb(timeout=60)
            print("[*] 1win feed ready: %s | BetBoom fallback ready: %s" % (ok_ow, ok_bb))
            if not ok_ow and not ok_bb:
                print("[!] neither 1win nor the enabled BetBoom fallback is ready — exiting")
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
    print("[*] bound 1win=%d BetBoom fallback=%d / %d"
          % (bound_ow, bound_bb, len(market_map)))
    _write_match_diagnostics(router, market_map)

    # ---------- CBR rate ----------
    cbr_rate = get_rate()
    rate = resolve_usd_rub_rate(cbr_rate, cfg.get("rate"))
    if rate is None:
        print("[!] no valid USD/RUB rate; set config.rate or restore CBR access")
        return 1
    if resolve_usd_rub_rate(cbr_rate, None) is None:
        print("[!] CBR unavailable; using configured USD/RUB = %.3f" % rate)
    else:
        print("[*] USD/RUB = %.3f" % rate)

    # ---------- PM feed ----------
    tokens = sorted({t for m in market_map.values() for t in (m.get("t") or [])})
    if not tokens:
        if not args.watch:
            print("[!] no tokens")
            return 1
        print("[!] no current PM tokens; watch mode will retry the catalog")
    print("[*] starting PM book feed (%d tokens)..." % len(tokens))
    feed = Feed(tokens)
    feed.start()
    time.sleep(2.0)
    last_pm_refresh = time.time()

    print("[*] main loop (Ctrl+C to stop), min_roi=%.2f%%" % (min_roi * 100))
    print()

    first_seen = {}
    last_alert = {}
    last_roi = {}
    last_sig = {}
    printed_once = set()
    upd_count = {}
    alert_gap = max(0.0, float(args.alert_gap))

    def retry_bind():
        if router is not None:
            with router._lock:
                src_now = set(router.bindings.keys())
            unbound = [s for s in market_map if s not in src_now]
        else:
            stream = get_global_stream()
            unbound = [s for s in market_map if s not in stream.bindings]
        if not unbound:
            _write_match_diagnostics(router, market_map)
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
        _write_match_diagnostics(router, market_map)
        return n

    last_retry = time.time()
    last_pm_refresh = time.time()
    scan_iteration = 0
    last_status_write = 0.0
    last_scan_status = {
        "state": "starting", "ts": time.time(), "cycle": 0,
        "pm_markets": len(market_map), "phase_counts": {}, "checks": {},
        "source_counts": {}, "pm_catalog_complete": pm_catalog_complete,
        "pm_catalog_retained": pm_catalog_retained,
        "min_roi_pct": round(min_roi * 100, 3),
    }
    _write_scan_status(last_scan_status)

    try:
        while not STOP.is_set():
            if args.watch and time.time() - last_pm_refresh >= args.watch_sec:
                fresh, refresh_complete = fetch_live_pm_markets(with_status=True)
                had_previous_catalog = bool(market_map)
                market_map, added, removed = reconcile_pm_market_map(
                    market_map, fresh, refresh_complete
                )
                pm_catalog_complete = refresh_complete
                pm_catalog_retained = not refresh_complete and had_previous_catalog
                if added or removed:
                    print("[*] watch: +%d / -%d markets (total %d)"
                          % (added, removed, len(market_map)))
                if not refresh_complete:
                    print("[!] PM catalog refresh partial; preserving prior entries until a complete refresh")
                refreshed_tokens = sorted({
                    str(token)
                    for market in market_map.values()
                    for token in (market.get("t") or [])
                    if token
                })
                if refreshed_tokens != tokens and feed.update_tokens(refreshed_tokens):
                    tokens = refreshed_tokens
                    print("[*] PM feed resubscribe: %d tokens" % len(tokens))
                last_pm_refresh = time.time()

            if time.time() - last_retry >= 10:
                retry_bind()
                last_retry = time.time()

            scan_iteration += 1
            phase_counts = {"live": 0, "prematch": 0, "unknown": 0}
            for _market in market_map.values():
                _phase = classify_event_phase(_market)
                phase_counts[_phase] = phase_counts.get(_phase, 0) + 1
            scan_counts = {
                "with_fresh_bookmaker": 0,
                "no_fresh_bookmaker": 0,
                "candidate_pairs": 0,
                "pm_no_ask": 0,
                "pm_depth_short": 0,
                "pm_impact_rejected": 0,
                "no_opposite_bk_market": 0,
                "invalid_market_or_no_quotes": 0,
                "invalid_calculation": 0,
                "pm_depth_rejects": 0,
                "below_roi": 0,
                "roi_passed": 0,
            }
            source_counts = {}
            live_snap = {}
            best_candidate_roi = None
            best_candidate_slug = None
            pm_no_ask_details = []
            for slug, m in market_map.items():
                if router is not None:
                    live_bc = router.live_bc(slug)
                    _bk_src = router.get_src(slug) or "BetBoom"
                else:
                    live_bc = get_live_bc(slug)
                    _bk_src = "BetBoom"
                if not live_bc:
                    scan_counts["no_fresh_bookmaker"] += 1
                    continue
                scan_counts["with_fresh_bookmaker"] += 1
                source_counts[_bk_src] = source_counts.get(_bk_src, 0) + 1

                candidate_diagnostics = {}
                pairs = build_arb_candidates(m, live_bc, slip, bank, rate,
                                             bk_source=_bk_src,
                                             diagnostics=candidate_diagnostics,
                                             pm_diagnostics=pm_no_ask_details,
                                             market_slug=slug)
                for _reason, _count in candidate_diagnostics.items():
                    scan_counts[_reason] = scan_counts.get(_reason, 0) + _count
                scan_counts["candidate_pairs"] += len(pairs)
                for bets in pairs:
                    r = compute(bets, bank)
                    if not r:
                        scan_counts["invalid_calculation"] += 1
                        continue
                    r = refine_stakes(bets, r, rate)
                    if not r:
                        scan_counts["pm_depth_rejects"] += 1
                        continue
                    roi = r["roi"]
                    if best_candidate_roi is None or roi > best_candidate_roi:
                        best_candidate_roi = roi
                        best_candidate_slug = slug
                    if roi < min_roi:
                        scan_counts["below_roi"] += 1
                        continue
                    scan_counts["roi_passed"] += 1
                    phase = classify_event_phase(m)

                    # --- web snapshot ---
                    try:
                        _prf = r["profit"]
                        _bb_b = None
                        _bb_stakes_debug = []
                        if router is not None:
                            try:
                                _bb_b = router.get_binding(slug)
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
                        _pm_leg = next((b for b in bets
                                        if b.get("source") == "Polymarket"), {})
                        _pm_age = _pm_leg.get("pm_age")
                        _bk_leg = next((b for b in bets
                                        if b.get("source") != "Polymarket"), {})
                        _bk_age = _bk_leg.get("odds_age_sec")
                        candidate_snapshot = {
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
                                    "pm_best_ask": b.get("pm_best_ask"),
                                    "pm_best_bid": b.get("pm_best_bid"),
                                    "pm_avg_ask": b.get("pm_avg_ask"),
                                    "odds_age_sec": b.get("odds_age_sec"),
                                } for b in bets
                            ],
                            "stakes": [float(x) for x in r["stakes"]],
                            "payout": float(r["payout"]),
                            "profit": float(_prf),
                            "phase": phase,
                            "is_live": phase == "live",
                            "is_streamer": bool(is_streamer(slug, m.get("q", ""))),
                            "pm_age": _pm_age,
                            "pm_best_ask": _pm_leg.get("pm_best_ask"),
                            "pm_best_bid": _pm_leg.get("pm_best_bid"),
                            "bk_source": _bk_src,
                            "bk_odds_age_sec": _bk_age,
                            "bk_event_url": (_bb_b or {}).get("event_url"),
                            "bk_match_id": (_bb_b or {}).get("match_id"),
                            "bk_binding": _bb_b,
                            "bb_match_id": ((_bb_b or {}).get("match_id")
                                            if _bk_src == "BetBoom" else None),
                            "bb_tournament_id": ((_bb_b or {}).get("tournament_id")
                                                 if _bk_src == "BetBoom" else None),
                            "bb_binding": _bb_b if _bk_src == "BetBoom" else None,
                            "bb_stakes_debug": _bb_stakes_debug,
                            "ts": time.time(),
                        }
                        current = live_snap.get(slug)
                        if current is None or candidate_snapshot["roi"] > current["roi"]:
                            live_snap[slug] = candidate_snapshot
                    except Exception:
                        pass
                    # --- /web snapshot ---

                    now = time.time()
                    _pm_signal_leg = next((b for b in bets
                                           if b.get("source") == "Polymarket"), {})
                    _bk_signal_leg = next((b for b in bets
                                           if b.get("source") != "Polymarket"), {})
                    signal_key = (
                        slug,
                        str(_pm_signal_leg.get("token") or ""),
                        str(_pm_signal_leg.get("outcome") or ""),
                        str(_bk_src),
                        str(_bk_signal_leg.get("outcome") or ""),
                    )
                    prev_sig = last_sig.get(signal_key)
                    prev_roi = last_roi.get(signal_key)

                    sig = (round(float(bets[0]["o"]), 2),
                           round(float(bets[1]["o"]), 2) if len(bets) > 1 else None)

                    changed = (prev_sig is None
                               or sig != prev_sig
                               or (prev_roi is not None and abs(roi - prev_roi) >= 0.005))
                    last_alert_ts = last_alert.get(signal_key)
                    if not alert_due(changed, last_alert_ts, now, alert_gap):
                        continue

                    if changed:
                        last_sig[signal_key] = sig
                        last_roi[signal_key] = roi
                    last_alert[signal_key] = now
                    fs = first_seen.setdefault(slug, now)

                    if signal_key not in printed_once:
                        printed_once.add(signal_key)
                        upd_count[signal_key] = 0
                        print_arb(m, bets, r, rate, fs, bk)
                    else:
                        upd_count[signal_key] = upd_count.get(signal_key, 0) + 1
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
                        profit_upd = r["profit"]
                        prefix = "~" if changed else "="
                        print("  [%s] %s [%s] upd#%-3d  PM %.2f/$%.2f  %s %.2f/%.0fR  ROI %+.1f%% (was %+.1f%%)  profit +%.0fR" % (
                            prefix,
                            slug[:38],
                            detect_market_type(slug, m.get("q", "")),
                            upd_count[signal_key],
                            pm_o, pm_st,
                            _bk_src, bk_o, bk_st,
                            roi * 100, prev_roi * 100 if prev_roi is not None else roi * 100,
                            profit_upd,
                        ))

                    if changed:
                        profit = r["profit"]
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
            pm_book_states = {}
            for _detail in pm_no_ask_details:
                _state = _detail.get("book_state", "unknown")
                pm_book_states[_state] = pm_book_states.get(_state, 0) + 1
            _write_json_atomic("pm_book_diagnostics.json", {
                "ts": _now,
                "cycle": scan_iteration,
                "summary": {
                    "pm_no_ask_markets": len(pm_no_ask_details),
                    "by_book_state": pm_book_states,
                },
                "markets": pm_no_ask_details,
            })
            if _now - globals().get('_last_snap_write', 0) >= 2.0:
                _write_json_atomic("arbs_live.json", live_snap)
                globals()['_last_snap_write'] = _now

            last_scan_status = {
                "state": "running",
                "ts": _now,
                "cycle": scan_iteration,
                "pm_markets": len(market_map),
                "phase_counts": phase_counts,
                "checks": scan_counts,
                "source_counts": source_counts,
                "pm_book_summary": {
                    "pm_no_ask_markets": len(pm_no_ask_details),
                    "by_book_state": pm_book_states,
                },
                "bookmaker_feed_health": router.feed_health() if router is not None else {},
                "pm_catalog_complete": pm_catalog_complete,
                "pm_catalog_retained": pm_catalog_retained,
                "signals": len(live_snap),
                "min_roi_pct": round(min_roi * 100, 3),
                "best_candidate_roi_pct": (
                    round(best_candidate_roi * 100, 3)
                    if best_candidate_roi is not None else None
                ),
                "best_candidate_slug": best_candidate_slug,
                "bank_rub": round(bank, 2),
                "usd_rub": round(rate, 4),
            }
            if _now - last_status_write >= 1.0:
                _write_scan_status(last_scan_status)
                last_status_write = _now

            time.sleep(poll)
    except KeyboardInterrupt:
        print()
        print("[*] stopped")
    finally:
        STOP.set()
        if last_scan_status:
            last_scan_status = dict(last_scan_status)
            last_scan_status["state"] = "stopped"
            last_scan_status["ts"] = time.time()
            _write_scan_status(last_scan_status)
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
