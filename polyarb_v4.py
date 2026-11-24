#!/usr/bin/env python3
# polyarb_v4.py - Polymarket/1win monitor; BetBoom remains an optional fallback.
# The scanner starts the read-only 1win odds adapter as its primary bookmaker source.
# v40: фикс dedup-алертов (больше нет спама одинаковых вилок).
# v41: интеграция bk_router (BB + 1win) — см. память проекта.
import argparse
import csv
from concurrent.futures import ThreadPoolExecutor, as_completed
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
from arb_invariants import is_pm_bookmaker_pair as _is_pm_bookmaker_pair
from arb_candidates import build_arb_candidates as _build_arb_candidates
from event_dispatch import (
    DirtyUpdateQueue,
    build_adapter_event_index,
    build_token_slug_index,
    resolve_dirty_slugs,
)
from event_phase import classify_event_phase
from pm_fees import (
    fetch_fee_schedule as fetch_pm_fee_schedule,
    fee_per_share as pm_fee_per_share,
)
from runtime_settings import (
    DEFAULT_RUNTIME_SETTINGS,
    RUNTIME_LIMIT_DEFAULTS,
    argparse_type as runtime_argparse_type,
    normalize_runtime_settings,
    resolve_setting,
    runtime_limits_payload,
)
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
    from bk_router import get_router, is_prematch_slug, normalize_enabled_sources
    ROUTER_ENABLED = True
    BB_ENABLED = True  # косвенно, чтобы старая логика тоже работала
except Exception as _router_err:
    _BB_ERR = _router_err
    def normalize_enabled_sources(sources, default=("1win",)):
        if not isinstance(sources, (list, tuple, set)):
            sources = default
        selected = set()
        for source in sources:
            key = str(source or "").strip().lower().replace(" ", "")
            if key in ("1win", "onewin"):
                selected.add("1win")
            elif key in ("betboom", "bb"):
                selected.add("BetBoom")
        return [name for name in ("1win", "BetBoom") if name in selected]
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
CLOB = HT + "clob" + DOT + "polymarket" + DOT + "com"
WS_URL = WSS + "ws-subscriptions-clob" + DOT + "polymarket" + DOT + "com" + "/ws/market"
WS_PING_INTERVAL_SEC = 8.0
MAX_STAKE_REFINEMENT_ITERATIONS = 5
CBR_URL = HT + "www" + DOT + "cbr" + DOT + "ru" + "/scripts/XML_daily.asp"
BANKROLL_PLAN_LIMIT = RUNTIME_LIMIT_DEFAULTS["bankroll_plan_fraction"]
PINNED_SIGNAL_TTL_SEC = 300.0
PINNED_SIGNAL_MIN_ROI = 0.05

TIMEOUT = 15.0
PM_CATALOG_TIMEOUT = (3.0, 5.0)
PM_CATALOG_WORKERS = RUNTIME_LIMIT_DEFAULTS["gamma_workers"]
# Gamma currently returns at most 100 events per page and omits next_cursor
# on terminal short pages, so use the observed page size as the safe boundary.
PM_CATALOG_PAGE_LIMIT = RUNTIME_LIMIT_DEFAULTS["gamma_page_limit"]
PM_CATALOG_MAX_PAGES = RUNTIME_LIMIT_DEFAULTS["gamma_max_pages_per_query"]
PM_CATALOG_MAX_RETRIES = RUNTIME_LIMIT_DEFAULTS["gamma_retries_per_page"]
PM_CATALOG_RETRYABLE_STATUSES = {429, 500, 502, 503, 504}
PM_CATALOG_RETRY_BACKOFF_SEC = 0.25
_PM_CATALOG_DIAGNOSTICS_LOCK = threading.Lock()
_PM_CATALOG_DIAGNOSTICS = {}
_PM_UPDATE_CALLBACK = None


def set_pm_update_callback(callback):
    """Register a non-blocking callback for committed PM book updates."""
    global _PM_UPDATE_CALLBACK
    _PM_UPDATE_CALLBACK = callback


def _notify_pm_update(token_id):
    callback = _PM_UPDATE_CALLBACK
    if callback is not None:
        try:
            callback(str(token_id))
        except Exception:
            pass


USDT = " USDT"


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
    "bank": DEFAULT_RUNTIME_SETTINGS["bank"],
    "currency": "USDT",
    "rate": 1.0,
    "poll": DEFAULT_RUNTIME_SETTINGS["poll"],
    "watch_sec": DEFAULT_RUNTIME_SETTINGS["watch_sec"],
    "alert_gap": DEFAULT_RUNTIME_SETTINGS["alert_gap"],
    "slip": 0.0,
    "live_min_roi_show": -3.0,
    "min_roi_pct": DEFAULT_RUNTIME_SETTINGS["min_roi_pct"],
    "roi_revision": 0,
    "enabled_bk_sources": ["1win"],
}
CONFIG_WARNINGS = []
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
    global CONFIG_WARNINGS
    if not os.path.exists(CFG_FILE):
        with open(CFG_FILE, "w", encoding="utf-8") as f:
            json.dump(DEFAULT_CFG, f, indent=2)
        CONFIG_WARNINGS = []
        return dict(DEFAULT_CFG)
    try:
        with open(CFG_FILE, encoding="utf-8") as f:
            c = json.load(f)
        if not isinstance(c, dict):
            raise ValueError("config root must be an object")
        o = dict(DEFAULT_CFG)
        o.update(c)
        normalized_source = dict(c)
        scan_cfg = c.get("scan") if isinstance(c.get("scan"), dict) else {}
        bot_cfg = c.get("bot") if isinstance(c.get("bot"), dict) else {}
        for key in ("bank", "min_roi_pct", "poll", "watch_sec", "alert_gap",
                    "safe_fork_threshold", "roi_revision"):
            if key not in normalized_source:
                if key in scan_cfg:
                    normalized_source[key] = scan_cfg[key]
                elif key in bot_cfg:
                    normalized_source[key] = bot_cfg[key]
        runtime_settings, warnings, legacy_migrated = normalize_runtime_settings(
            normalized_source
        )
        o.update(runtime_settings)
        o["currency"] = "USDT"
        o["rate"] = 1.0
        CONFIG_WARNINGS = warnings
        o["enabled_bk_sources"] = normalize_enabled_sources(
            o.get("enabled_bk_sources", ["1win"])
        ) or ["1win"]
        if legacy_migrated and not warnings:
            with open(CFG_FILE, "w", encoding="utf-8") as f:
                json.dump(o, f, ensure_ascii=False, indent=2)
        return o
    except Exception as exc:
        CONFIG_WARNINGS = [
            "Не удалось прочитать config.json; используются безопасные настройки "
            f"по умолчанию ({type(exc).__name__})."
        ]
        return dict(DEFAULT_CFG)


def select_runtime_sources(configured, available, *, no_bb=False):
    """Select only available feeds while preserving the launcher's hard --no-bb deny."""
    wanted = normalize_enabled_sources(configured)
    available_set = set(normalize_enabled_sources(available, default=()))
    if no_bb:
        wanted = [source for source in wanted if source != "BetBoom"]
    selected = [source for source in wanted if source in available_set]
    if not selected:
        selected = [source for source in ("1win", "BetBoom")
                    if source in available_set and (source != "BetBoom" or not no_bb)][:1]
    return selected


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


def log_arb_json(slug, sport, roi, bank, profit, payout_usdt, bets, stakes,
                 m, rate, first_seen_ts=0.0):
    try:
        legs = []
        for i, b in enumerate(bets):
            stake_usdt = float(stakes[i]) if i < len(stakes) else 0.0
            src = b.get("source", "")
            legs.append({
                "book": src,
                "selection": b.get("outcome", ""),
                "odds": round(float(b.get("o", 0)), 4),
                "stake_native": round(stake_usdt, 2),
                "currency": "USDT",
                "stake_usdt": round(stake_usdt, 2),
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
            "currency": "USDT",
            "bank_usdt": round(bank, 2),
            "plan_exposure_usdt": round(sum(float(x) for x in stakes), 2),
            "risk_limit_pct": round(BANKROLL_PLAN_LIMIT * 100.0, 2),
            "profit_usdt": round(profit, 2),
            "payout_usdt": round(payout_usdt, 2),
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


# PM market-data implementation lives in pm_market_data; these facade names
# intentionally remain patchable for legacy callers/tests.
import pm_market_data as _pm_market_data
SIGNAL_DROP_HISTORY_TTL_SEC = 300.0
MAX_RECENT_SIGNAL_DROPS = 10
BOOKS = _pm_market_data.BOOKS
LOCK = _pm_market_data.LOCK
STOP = _pm_market_data.STOP
BOOK_TTL = _pm_market_data.BOOK_TTL
_PM_REFRESH_EXECUTOR = _pm_market_data._PM_REFRESH_EXECUTOR
_PM_REFRESH_LOCK = _pm_market_data._PM_REFRESH_LOCK
_PM_REFRESH_INFLIGHT = _pm_market_data._PM_REFRESH_INFLIGHT
_PM_REFRESH_COOLDOWN = _pm_market_data._PM_REFRESH_COOLDOWN
_PM_REFRESH_DIAGNOSTICS = _pm_market_data._PM_REFRESH_DIAGNOSTICS
_FRESH_MIN_INTERVAL = _pm_market_data._FRESH_MIN_INTERVAL
_STALE_THRESHOLD = _pm_market_data._STALE_THRESHOLD
for _name in ("WS_URL", "CLOB", "WS_PING_INTERVAL_SEC"):
    globals()[_name] = getattr(_pm_market_data, _name)
_pm_market_data.HAS_WS = HAS_WS
_pm_market_data.websocket = websocket
_parse_clob_event_timestamp = _pm_market_data._parse_clob_event_timestamp
_book_source_timestamp = _pm_market_data._book_source_timestamp
_parse_reported_top_price = _pm_market_data._parse_reported_top_price
_reported_tops_match = _pm_market_data._reported_tops_match

def _notify_pm_update(token_id):
    callback = _PM_UPDATE_CALLBACK
    if callback is not None:
        try: callback(str(token_id))
        except Exception: pass

class Feed(_pm_market_data.Feed):
    def __init__(self, ids):
        super().__init__(ids, on_update=_notify_pm_update, schedule_refresh=_schedule_pm_book_refresh)

def _fresh_pm_book(token_id, timeout=6.0):
    return _pm_market_data._fresh_pm_book(token_id, timeout)

def _refresh_pm_book_worker(token_id, attempted_at, reason):
    return _pm_market_data._refresh_pm_book_worker(
        token_id, attempted_at, reason, fresh_fetch=_fresh_pm_book, notify_update=_notify_pm_update)

def _schedule_pm_book_refresh(token_id, reason):
    return _pm_market_data._schedule_pm_book_refresh(
        token_id, reason, executor=_PM_REFRESH_EXECUTOR,
        fresh_fetch=_fresh_pm_book, notify_update=_notify_pm_update)

best_ask = _pm_market_data.best_ask
best_bid = _pm_market_data.best_bid
walk_book = _pm_market_data.walk_book
walk_book_with_fee = _pm_market_data.walk_book_with_fee
pm_age_sec = _pm_market_data.pm_age_sec
pm_book_diagnostic = _pm_market_data.pm_book_diagnostic
pm_refresh_attempted = _pm_market_data.pm_refresh_attempted
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


def _risk_capped_plan_bank(total_bank):
    try:
        bank = float(total_bank)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(bank) or bank <= 0:
        return 0.0
    return bank * BANKROLL_PLAN_LIMIT


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
    if not result:
        return None
    bank = sum(float(x) for x in result.get("stakes", []))
    if not math.isfinite(bank) or bank <= 0:
        return None
    for _ in range(MAX_STAKE_REFINEMENT_ITERATIONS):
        changed = False
        for i, b in enumerate(bets):
            if b.get("source") != "Polymarket":
                continue
            stake_usdt = result["stakes"][i]
            if stake_usdt <= 0:
                continue
            ba = best_ask(b.get("token", ""))
            if ba <= 0:
                return None
            try:
                fee_rate = float(b["pm_fee_rate"])
                fee_exponent = float(b["pm_fee_exponent"])
            except (KeyError, TypeError, ValueError, OverflowError):
                return None
            depth_quote = walk_book_with_fee(
                b.get("token", ""), stake_usdt, fee_rate, fee_exponent
            )
            if not depth_quote:
                return None
            avg = depth_quote["avg_price"]
            shares = depth_quote["shares"]
            # Preserve the initial candidate-sizing probe fields, but publish
            # the depth price/size used by this stake-refinement iteration.
            b["pm_refined_avg_ask"] = float(avg)
            b["pm_refined_shares"] = float(shares)
            b["pm_refined_stake_usd"] = float(stake_usdt)
            # Both legs are denominated in USDT; a PM share costs its ask in
            # USDT-equivalent and pays one USDT per winning share.
            b["pm_fee_estimated_usd"] = depth_quote["fee"]
            new_o = depth_quote["effective_multiplier"]
            if abs(new_o - b["o"]) / b["o"] > 0.0005:
                b["o"] = new_o
                changed = True
        if not changed:
            break
        result = compute(bets, bank)
        if result is None:
            return None
    # The last recomputed allocation has not been repriced against the PM
    # quote it implies. Reject rather than publish mismatched quote and stakes.
    if changed:
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
            "condition_id": m.get("conditionId"),
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


def _fetch_pm_catalog_query(tag, mode, page_limit=PM_CATALOG_PAGE_LIMIT):
    """Fetch one tag/mode stream to cursor exhaustion, or report it as partial."""
    events = []
    cursor = None
    seen_cursors = set()
    pages = 0
    raw_event_rows = 0
    invalid_event_rows = 0
    effective_page_limit = int(page_limit)
    last_http_status = None
    error = None
    cursor_exhausted = False
    page_cap_hit = False
    limit_downgraded = False

    while pages < PM_CATALOG_MAX_PAGES:
        params = {
            "tag_slug": tag,
            "limit": effective_page_limit,
            "active": "true",
            "closed": "false",
        }
        if mode == "live":
            params["live"] = "true"
        if cursor is not None:
            params["after_cursor"] = cursor

        response = None
        fallback_limit = False
        for attempt in range(PM_CATALOG_MAX_RETRIES):
            try:
                response = requests.get(
                    GAMMA + "/events/keyset",
                    params=params,
                    timeout=PM_CATALOG_TIMEOUT,
                )
                last_http_status = getattr(response, "status_code", None)
                response.raise_for_status()
                break
            except Exception as exc:
                exc_response = getattr(exc, "response", None)
                status = getattr(exc_response, "status_code", None)
                if status is None and response is not None:
                    status = getattr(response, "status_code", None)
                last_http_status = status or last_http_status
                if status == 400 and effective_page_limit > 100 and not limit_downgraded:
                    effective_page_limit = 100
                    limit_downgraded = True
                    fallback_limit = True
                    response = None
                    break
                retryable_status = status in PM_CATALOG_RETRYABLE_STATUSES
                retryable_exception = isinstance(
                    exc, (requests.ConnectionError, requests.Timeout, OSError)
                )
                if (attempt + 1 < PM_CATALOG_MAX_RETRIES
                        and (retryable_status or retryable_exception)):
                    time.sleep(PM_CATALOG_RETRY_BACKOFF_SEC * (2 ** attempt))
                    continue
                error = "HTTP_%s" % status if status else type(exc).__name__
                response = None
                break

        if fallback_limit:
            # Retry the same cursor with a smaller page size; do not count a page.
            continue
        if response is None:
            break

        try:
            payload = response.json()
        except Exception:
            error = "InvalidJSON"
            break
        if not isinstance(payload, dict):
            error = "InvalidResponseShape"
            break
        page_events = payload.get("events")
        if not isinstance(page_events, list):
            error = "InvalidEventsShape"
            break

        pages += 1
        raw_event_rows += len(page_events)
        valid_events = [event for event in page_events if isinstance(event, dict)]
        invalid_event_rows += len(page_events) - len(valid_events)
        events.extend(valid_events)

        if "next_cursor" not in payload:
            if len(page_events) < effective_page_limit:
                # Gamma currently omits this field on terminal short pages.
                cursor_exhausted = True
            else:
                # A full page without a cursor could be silently truncated.
                error = "MissingNextCursor"
            break

        next_cursor = payload.get("next_cursor")
        cursor_text = str(next_cursor).strip() if next_cursor is not None else ""
        if next_cursor is None or cursor_text in {"", "LTE="}:
            cursor_exhausted = True
            break
        if cursor_text == cursor or cursor_text in seen_cursors:
            error = "RepeatedCursor"
            break
        seen_cursors.add(cursor_text)
        cursor = cursor_text

    if pages >= PM_CATALOG_MAX_PAGES and not cursor_exhausted and not error:
        page_cap_hit = True
        error = "PageCapReached"

    return {
        "tag": tag,
        "mode": mode,
        "pages": pages,
        "raw_event_rows": raw_event_rows,
        "invalid_event_rows": invalid_event_rows,
        "events": events,
        "cursor_exhausted": cursor_exhausted,
        "page_cap_hit": page_cap_hit,
        "effective_page_limit": effective_page_limit,
        "last_http_status": last_http_status,
        "error": error,
    }


def _fetch_pm_catalog_tag(tag, query_limit=PM_CATALOG_PAGE_LIMIT):
    results = [
        _fetch_pm_catalog_query(tag, mode, query_limit)
        for mode in ("live", "general")
    ]
    return tag, results


def get_pm_catalog_diagnostics():
    with _PM_CATALOG_DIAGNOSTICS_LOCK:
        snapshot = dict(_PM_CATALOG_DIAGNOSTICS)
    snapshot["error_counts"] = dict(snapshot.get("error_counts") or {})
    snapshot["failed_tags"] = list(snapshot.get("failed_tags") or [])
    snapshot["queries"] = [dict(query) for query in snapshot.get("queries") or []]
    return snapshot


def fetch_live_pm_markets(with_status=False):
    tags = ["esports", "cs2", "dota-2", "league-of-legends",
            "valorant", "tennis", "atp"]
    prefixes = ("cs2-", "dota2-", "lol-", "valorant-",
                "atp-", "wta-", "itf-")
    started = time.monotonic()
    out = {}
    query_count = len(tags) * 2
    successful_queries = 0
    cursor_exhausted_queries = 0
    total_pages = 0
    page_cap_queries = 0
    invalid_event_rows = 0
    catalog_complete = True
    results_by_tag = {}
    error_counts = {}
    failed_tags = set()
    query_details = []

    # Keep the existing per-tag worker bound; each worker walks the live and
    # general keysets serially so cursor order is preserved within a stream.
    with ThreadPoolExecutor(
        max_workers=min(PM_CATALOG_WORKERS, len(tags)),
        thread_name_prefix="pm-catalog",
    ) as executor:
        futures = {
            executor.submit(_fetch_pm_catalog_tag, tag, PM_CATALOG_PAGE_LIMIT): tag
            for tag in tags
        }
        for future in as_completed(futures):
            tag = futures[future]
            try:
                result_tag, results = future.result()
                results_by_tag[result_tag] = results
            except Exception as exc:
                error_type = type(exc).__name__
                results_by_tag[tag] = [
                    {
                        "tag": tag, "mode": mode, "pages": 0,
                        "raw_event_rows": 0, "invalid_event_rows": 0,
                        "events": [], "cursor_exhausted": False,
                        "page_cap_hit": False,
                        "effective_page_limit": PM_CATALOG_PAGE_LIMIT,
                        "last_http_status": None, "error": error_type,
                    }
                    for mode in ("live", "general")
                ]

    for tag in tags:
        for result in results_by_tag.get(tag, []):
            pages = int(result.get("pages") or 0)
            total_pages += pages
            invalid_event_rows += int(result.get("invalid_event_rows") or 0)
            cursor_exhausted = bool(result.get("cursor_exhausted"))
            query_error = result.get("error")
            if cursor_exhausted:
                cursor_exhausted_queries += 1
            if result.get("page_cap_hit"):
                page_cap_queries += 1
            if not cursor_exhausted or query_error:
                catalog_complete = False
                failed_tags.add(tag)
                error_type = query_error or "CursorNotExhausted"
                error_counts[error_type] = error_counts.get(error_type, 0) + 1
            else:
                successful_queries += 1
            if result.get("invalid_event_rows"):
                catalog_complete = False

            query_details.append({
                "tag": tag,
                "mode": result.get("mode"),
                "pages": pages,
                "raw_event_rows": int(result.get("raw_event_rows") or 0),
                "cursor_exhausted": cursor_exhausted,
                "page_cap_hit": bool(result.get("page_cap_hit")),
                "effective_page_limit": result.get("effective_page_limit"),
                "last_http_status": result.get("last_http_status"),
                "error": query_error,
            })

            for event in result.get("events") or []:
                if not isinstance(event, dict):
                    catalog_complete = False
                    continue
                event_markets = event.get("markets") or []
                if not isinstance(event_markets, list):
                    catalog_complete = False
                    continue
                for market in event_markets:
                    if not isinstance(market, dict):
                        catalog_complete = False
                        continue
                    slug = market.get("slug", "")
                    if not slug or not slug.startswith(prefixes):
                        continue
                    if market.get("closed") is True:
                        continue
                    if market.get("acceptingOrders") is False:
                        continue
                    try:
                        outcomes = json.loads(market.get("outcomes", "[]"))
                        tokens = json.loads(market.get("clobTokenIds", "[]"))
                    except Exception:
                        catalog_complete = False
                        continue
                    if (
                        not isinstance(outcomes, list) or not isinstance(tokens, list)
                        or len(outcomes) != 2 or len(tokens) != 2
                        or any(not isinstance(value, str) or not value.strip()
                               for value in outcomes + tokens)
                    ):
                        catalog_complete = False
                        continue
                    start = (market.get("eventStartTime") or event.get("startTime") or "")
                    phase = classify_event_phase(market, event)
                    out[slug] = {
                        "slug": slug, "condition_id": market.get("conditionId"),
                        "q": market.get("question", ""),
                        "o": outcomes, "t": tokens, "start": start,
                        "phase": phase,
                        "_phase_classified": True,
                        "live": phase == "live",
                        "status": market.get("status") or event.get("status"),
                        "closed": market.get("closed", event.get("closed")),
                        "active": market.get("active", True),
                    }

    complete = query_count > 0 and successful_queries == query_count and catalog_complete
    diagnostics = {
        "query_count": query_count,
        "successful_queries": successful_queries,
        "failed_queries": query_count - successful_queries,
        "cursor_exhausted_queries": cursor_exhausted_queries,
        "total_pages": total_pages,
        "page_limit": PM_CATALOG_PAGE_LIMIT,
        "max_pages_per_query": PM_CATALOG_MAX_PAGES,
        "page_cap_queries": page_cap_queries,
        "invalid_event_rows": invalid_event_rows,
        "error_counts": error_counts,
        "failed_tags": sorted(failed_tags),
        "queries": query_details,
        "markets": len(out),
        "complete": complete,
        "elapsed_ms": round((time.monotonic() - started) * 1000, 1),
    }
    with _PM_CATALOG_DIAGNOSTICS_LOCK:
        _PM_CATALOG_DIAGNOSTICS.clear()
        _PM_CATALOG_DIAGNOSTICS.update(diagnostics)
    if diagnostics["failed_queries"] or not catalog_complete:
        errors = ", ".join(
            "%s=%s" % (name, count)
            for name, count in sorted(error_counts.items())
        ) or "invalid catalog data"
        print(
            "[!] Polymarket Gamma catalog: %d/%d query streams fully paginated; "
            "%d incomplete (%s), %d pages, %d markets in %.0f ms" % (
                successful_queries, query_count, diagnostics["failed_queries"],
                errors, total_pages, len(out), diagnostics["elapsed_ms"],
            ),
            flush=True,
        )
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

        stake_str = "%.2f USDT" % st

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
    print("  Profit:   %s%.2f%s%s" % (C.G, profit_b, USDT, C.RST))
    print("  Payout:   %s%.2f%s%s" % (C.W, r["payout"], USDT, C.RST))
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
    """Compatibility facade; candidate construction lives in arb_candidates."""
    return _build_arb_candidates(
        m, live_bc, slip, bank, rate, bk_source, diagnostics,
        pm_diagnostics, market_slug,
        fetch_pm_fee_schedule=fetch_pm_fee_schedule,
        maybe_refresh_stale=_maybe_refresh_stale,
        best_ask=best_ask,
        walk_book_with_fee=walk_book_with_fee,
        pm_age_sec=pm_age_sec,
        best_bid=best_bid,
        pm_refresh_attempted=pm_refresh_attempted,
        pm_book_diagnostic=pm_book_diagnostic,
    )


def _classify_signal_drop_reason(*, market_missing=False, has_fresh_bookmaker=False,
                                 candidate_diagnostics=None, best_roi=None,
                                 min_roi=0.0, depth_rejects=0,
                                 invalid_calculations=0, pm_diagnostic=None):
    if market_missing:
        return "market_removed"
    if not has_fresh_bookmaker:
        return "no_fresh_bookmaker_pair"
    if best_roi is not None and best_roi < min_roi:
        return "below_roi_threshold"
    diagnostics = candidate_diagnostics or {}
    book_state = (pm_diagnostic or {}).get("book_state")
    if book_state == "stale_book":
        return "pm_book_stale"
    if book_state in {"missing_book", "missing_timestamp"}:
        return "pm_book_missing"
    if book_state == "empty_book":
        return "pm_book_empty"
    if book_state == "empty_asks":
        return "pm_ask_unavailable"
    if diagnostics.get("pm_no_ask", 0):
        return "pm_ask_unavailable"
    if diagnostics.get("pm_depth_short", 0):
        return "pm_depth_short"
    if diagnostics.get("pm_impact_rejected", 0):
        return "pm_impact_rejected"
    if depth_rejects:
        return "pm_depth_rejected"
    if diagnostics.get("no_opposite_bk_market", 0):
        return "no_opposite_bookmaker_outcome"
    if invalid_calculations or diagnostics.get("invalid_market_or_no_quotes", 0):
        return "invalid_candidate"
    return "no_valid_candidate"


def _make_signal_drop_diagnostic(snapshot, reason, dropped_at,
                                 min_roi=0.0, current_best_roi=None,
                                 current_pm_diagnostic=None):
    snapshot = snapshot or {}
    bets = snapshot.get("bets") or []
    pm_leg = next((bet for bet in bets if bet.get("source") == "Polymarket"), {})
    bookmaker_leg = next((bet for bet in bets if bet.get("source") != "Polymarket"), {})

    def number(value, multiplier=1.0):
        try:
            result = float(value) * multiplier
        except (TypeError, ValueError):
            return None
        return round(result, 3) if math.isfinite(result) else None

    previous_roi = number(snapshot.get("roi"), 100.0)
    snapshot_ts = number(snapshot.get("ts"))
    current_pm_diagnostic = current_pm_diagnostic or {}
    current_pm_book = None
    if current_pm_diagnostic:
        current_pm_book = {
            "state": str(current_pm_diagnostic.get("book_state") or "unknown"),
            "age_sec": number(current_pm_diagnostic.get("book_age_sec")),
            "bid_levels": number(current_pm_diagnostic.get("bid_levels")),
            "ask_levels": number(current_pm_diagnostic.get("ask_levels")),
            "best_bid": number(current_pm_diagnostic.get("best_bid")),
            "best_ask": number(current_pm_diagnostic.get("best_ask")),
            "rest_refresh": str(current_pm_diagnostic.get("rest_refresh") or ""),
        }
    return {
        "slug": str(snapshot.get("slug") or ""),
        "q": str(snapshot.get("q") or "")[:180],
        "sport": str(snapshot.get("sport") or ""),
        "market_type": str(snapshot.get("market_type") or ""),
        "reason": str(reason),
        "dropped_at": number(dropped_at),
        "signal_snapshot_age_sec": round(max(0.0, float(dropped_at) - snapshot_ts), 1)
        if snapshot_ts is not None else None,
        "previous_roi_pct": previous_roi,
        "current_best_roi_pct": number(current_best_roi, 100.0),
        "min_roi_pct": number(min_roi, 100.0),
        "pm_quote": {
            "outcome": str(pm_leg.get("outcome") or ""),
            "effective_odds": number(pm_leg.get("o")),
            "best_ask": number(pm_leg.get("pm_best_ask")),
            "average_ask": number(pm_leg.get("pm_avg_ask")),
            "best_bid": number(pm_leg.get("pm_best_bid")),
            "book_age_sec": number(pm_leg.get("pm_book_age_sec", pm_leg.get("pm_age"))),
            "target_usd": number(pm_leg.get("pm_target_usd")),
            "target_shares": number(pm_leg.get("pm_target_shares")),
        },
        "current_pm_book": current_pm_book,
        "bookmaker_quote": {
            "source": str(bookmaker_leg.get("source") or snapshot.get("bk_source") or ""),
            "outcome": str(bookmaker_leg.get("outcome") or ""),
            "odds": number(bookmaker_leg.get("o")),
            "age_sec": number(bookmaker_leg.get("odds_age_sec")),
        },
    }


def _prune_recent_signal_drops(drops, now):
    for slug, row in list(drops.items()):
        try:
            dropped_at = float(row.get("dropped_at") or 0.0)
        except (AttributeError, TypeError, ValueError):
            dropped_at = 0.0
        if (not math.isfinite(dropped_at) or dropped_at <= 0.0
                or now - dropped_at > SIGNAL_DROP_HISTORY_TTL_SEC):
            drops.pop(slug, None)
    ordered = sorted(
        drops.items(),
        key=lambda item: float(item[1].get("dropped_at") or 0.0),
        reverse=True,
    )
    for slug, _row in ordered[MAX_RECENT_SIGNAL_DROPS:]:
        drops.pop(slug, None)
    return [row for _slug, row in ordered[:MAX_RECENT_SIGNAL_DROPS]]


def _serialize_candidate_bets(bets):
    return [
        {
            "source": bet.get("source"),
            "outcome": bet.get("outcome"),
            "token": bet.get("token"),
            "o": float(bet.get("o") or 0),
            "c": float(bet.get("c") or 0),
            "s": float(bet.get("s") or 0),
            "pm_fee_enabled": bet.get("pm_fee_enabled"),
            "pm_fee_rate": bet.get("pm_fee_rate"),
            "pm_fee_exponent": bet.get("pm_fee_exponent"),
            "pm_fee_source": bet.get("pm_fee_source"),
            "pm_fee_estimated_usd": bet.get("pm_fee_estimated_usd"),
            "pm_age": bet.get("pm_age"),
            "pm_best_ask": bet.get("pm_best_ask"),
            "pm_best_bid": bet.get("pm_best_bid"),
            "pm_avg_ask": bet.get("pm_avg_ask"),
            "pm_book_age_sec": bet.get("pm_book_age_sec"),
            "pm_target_usd": bet.get("pm_target_usd"),
            "pm_target_shares": bet.get("pm_target_shares"),
            "pm_refined_avg_ask": bet.get("pm_refined_avg_ask"),
            "pm_refined_shares": bet.get("pm_refined_shares"),
            "pm_refined_stake_usd": bet.get("pm_refined_stake_usd"),
            "odds_age_sec": bet.get("odds_age_sec"),
        }
        for bet in bets
    ]


def _pinned_signal_key(snapshot):
    bets = snapshot.get("bets") or []
    pm_leg = next((leg for leg in bets if leg.get("source") == "Polymarket"), {})
    bk_leg = next((leg for leg in bets if leg.get("source") != "Polymarket"), {})
    return "|".join((
        str(snapshot.get("slug") or ""),
        str(pm_leg.get("token") or ""),
        str(pm_leg.get("outcome") or ""),
        str(bk_leg.get("source") or ""),
        str(bk_leg.get("outcome") or ""),
    ))


def _prune_pinned_signal_snapshots(snapshots, now):
    for key, row in list(snapshots.items()):
        try:
            expires_at = float(row.get("expires_at") or 0.0)
        except (AttributeError, TypeError, ValueError):
            expires_at = 0.0
        if not math.isfinite(expires_at) or expires_at <= now:
            snapshots.pop(key, None)
    ordered = sorted(
        snapshots.items(),
        key=lambda item: float(item[1].get("locked_at") or 0.0),
        reverse=True,
    )
    return [row for _key, row in ordered]


def _pin_signal_snapshot(snapshots, snapshot, now=None):
    now = time.time() if now is None else float(now)
    _prune_pinned_signal_snapshots(snapshots, now)
    try:
        roi = float(snapshot.get("roi"))
    except (AttributeError, TypeError, ValueError):
        return False
    if not math.isfinite(roi) or roi < PINNED_SIGNAL_MIN_ROI:
        return False
    key = _pinned_signal_key(snapshot)
    if not key or key in snapshots:
        return False
    frozen = json.loads(json.dumps(snapshot, ensure_ascii=False))
    frozen["snapshot_id"] = "%s|%.6f" % (key, now)
    frozen["currency"] = "USDT"
    frozen["locked_at"] = now
    frozen["expires_at"] = now + PINNED_SIGNAL_TTL_SEC
    frozen["snapshot_roi_pct"] = round(roi * 100.0, 3)
    frozen["inactive_snapshot"] = True
    snapshots[key] = frozen
    _prune_pinned_signal_snapshots(snapshots, now)
    return True


# =================================================================
#                              main
# =================================================================

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--slugs", default="", help="comma-separated PM slugs")
    ap.add_argument("--live", action="store_true",
                    help="deprecated legacy flag; Gamma metadata determines event phase")
    ap.add_argument("--poll", type=runtime_argparse_type("poll"), default=None,
                    help="seconds between checks; CLI override of config.json")
    ap.add_argument("--bank", type=runtime_argparse_type("bank"), default=None,
                    help="override total bank size (USDT)")
    ap.add_argument("--min-roi", type=runtime_argparse_type("min_roi_pct"), default=None,
                    help="initial ROI threshold in %% (dashboard changes apply live)")
    ap.add_argument("--no-bb", action="store_true", help="disable BetBoom fallback; keep 1win")
    ap.add_argument("--watch", action="store_true", help="auto-refresh PM live markets")
    ap.add_argument("--watch-sec", type=runtime_argparse_type("watch_sec"), default=None,
                    help="watch refresh interval; CLI override of config.json")
    ap.add_argument("--alert-gap", type=runtime_argparse_type("alert_gap"), default=None,
                    help="minimum gap between signal updates and repeat reminders")
    args = ap.parse_args()
    if args.live:
        print("[!] --live is deprecated and ignored; event phase requires source evidence")

    cfg = load_cfg()
    runtime_limits = runtime_limits_payload()
    startup_warnings = list(CONFIG_WARNINGS) + list(runtime_limits.get("warnings", []))
    for warning in startup_warnings:
        print("[!] Настройки: " + warning)
    bk  = load_bk()
    init_db()

    bank, bank_source = resolve_setting(cfg, args.bank, "bank")
    slip    = float(cfg.get("slip", 0.0))
    poll, poll_source = resolve_setting(cfg, args.poll, "poll")
    watch_sec, watch_source = resolve_setting(cfg, args.watch_sec, "watch_sec")
    alert_gap, alert_gap_source = resolve_setting(cfg, args.alert_gap, "alert_gap")
    min_roi_pct, roi_source = resolve_setting(cfg, args.min_roi, "min_roi_pct")
    min_roi = min_roi_pct / 100.0
    runtime_setting_sources = {
        "bank": bank_source,
        "min_roi_pct": roi_source,
        "poll": poll_source,
        "watch_sec": watch_source,
        "alert_gap": alert_gap_source,
    }
    try:
        roi_revision = int(cfg.get("roi_revision", 0))
    except (TypeError, ValueError):
        roi_revision = 0

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

    if args.watch:
        _write_scan_status({
            "state": "starting",
            "ts": time.time(),
            "cycle": 0,
            "update_mode": "event",
            "processed_markets": 0,
            "source_updates": 0,
            "pending_updates": 0,
            "pm_markets": len(market_map),
            "phase_counts": {"live": 0, "prematch": 0, "unknown": 0},
            "checks": {},
            "source_counts": {},
            "pm_catalog_complete": pm_catalog_complete,
            "pm_catalog_retained": pm_catalog_retained,
            "pm_catalog_fetch": get_pm_catalog_diagnostics(),
            "signals": 0,
            "min_roi_pct": round(min_roi * 100, 3),
            "bank_usdt": round(bank, 2),
            "currency": "USDT",
            "runtime_settings": {
                "bank_usdt": round(bank, 2),
                "min_roi_pct": round(min_roi * 100, 3),
                "poll_sec": poll,
                "watch_sec": watch_sec,
                "alert_gap_sec": alert_gap,
                "sources": dict(runtime_setting_sources),
            },
            "runtime_limits": runtime_limits,
            "config_warnings": list(CONFIG_WARNINGS) + list(runtime_limits.get("warnings", [])),
        })

    # ---------- BK bridge: 1win primary; BetBoom is a fallback ----------
    update_queue = DirtyUpdateQueue()
    set_pm_update_callback(update_queue.put_pm_token)
    router = None
    if ROUTER_ENABLED:
        print("[*] starting bookmaker router (1win primary; BetBoom fallback %s)..."
              % ("disabled" if args.no_bb else "enabled"))
        try:
            router = get_router(use_bb=not args.no_bb, use_onewin=True)
            selected_sources = select_runtime_sources(
                cfg.get("enabled_bk_sources", ["1win"]),
                router.available_sources(),
                no_bb=args.no_bb,
            )
            router.configure_sources(selected_sources)
            router.set_adapter_update_callback(update_queue.put_adapter_event)
            router.start()
            ok_ow = router.wait_onewin(timeout=60, min_matches=1)
            ok_bb = False
            if not ok_ow and router.use_bb:
                ok_bb = router.wait_bb(timeout=60)
            print("[*] 1win feed ready: %s | BetBoom fallback ready: %s" % (ok_ow, ok_bb))
            if not ok_ow and not ok_bb:
                if args.watch:
                    print("[!] no selected bookmaker feed is ready yet; watch mode stays up "
                          "so feeds can be changed from the dashboard")
                else:
                    print("[!] neither selected bookmaker feed is ready — exiting")
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

    # Both bookmaker stakes and PM shares are displayed and calculated in USDT.
    rate = 1.0
    print("[*] stake currency: USDT (1 USDT assumed as 1 USD-equivalent for PM quotes)")

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
    token_index = build_token_slug_index(market_map)
    adapter_event_index = (
        router.adapter_event_index() if router is not None else {}
    )

    def refresh_event_indexes():
        token_index.clear()
        token_index.update(build_token_slug_index(market_map))
        adapter_event_index.clear()
        if router is not None:
            adapter_event_index.update(router.adapter_event_index())

    newly_bound_slugs = set()

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
                        newly_bound_slugs.add(slug)
                else:
                    if bind_slug(slug, market_map[slug]):
                        n += 1
                        newly_bound_slugs.add(slug)
            except Exception:
                pass
        refresh_event_indexes()
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
        "update_mode": "event",
        "pm_markets": len(market_map), "phase_counts": {}, "checks": {},
        "source_counts": {}, "pm_catalog_complete": pm_catalog_complete,
        "pm_catalog_retained": pm_catalog_retained,
        "pm_catalog_fetch": get_pm_catalog_diagnostics(),
        "recent_signal_drops": [],
        "pinned_signal_snapshots": [],
        "currency": "USDT",
        "min_roi_pct": round(min_roi * 100, 3),
        "runtime_limits": runtime_limits,
        "config_warnings": list(CONFIG_WARNINGS) + list(runtime_limits.get("warnings", [])),
    }
    _write_scan_status(last_scan_status)

    publisher = None
    publisher_token = os.environ.get("POLYARB_SCANNER_WS_TOKEN")
    if publisher_token and HAS_WS:
        try:
            from scanner_publisher import ScannerPublisher
            publisher = ScannerPublisher(
                "ws://127.0.0.1:8080/ws/scanner",
                publisher_token,
                websocket_module=websocket,
                verbose=True,
            )
        except Exception as exc:
            print("[!] dashboard WebSocket publisher unavailable: %s" % type(exc).__name__)

    live_snap = {}
    recent_signal_drops = {}
    pinned_signal_snapshots = {}
    best_by_slug = {}
    best_by_slug_updated_at = {}
    initial_dirty = set(market_map)
    last_fallback_refresh = 0.0
    last_expiry_sweep = 0.0
    last_publish = 0.0
    last_phase_refresh = 0.0
    last_router_health_refresh = 0.0
    last_runtime_config_check = 0.0
    router_stats_cache = {}
    router_feed_health_cache = {}
    phase_counts_cache = {"live": 0, "prematch": 0, "unknown": 0}
    last_pm_diag_write = 0.0

    try:
        while not STOP.is_set():
            now = time.time()
            runtime_sources_changed = False
            if now - last_runtime_config_check >= 1.0:
                last_runtime_config_check = now
                try:
                    runtime_cfg = load_cfg()
                    if args.bank is None:
                        configured_bank = float(runtime_cfg.get("bank", bank))
                        if math.isfinite(configured_bank) and configured_bank > 0 \
                                and abs(configured_bank - bank) >= 0.005:
                            bank = configured_bank
                            runtime_setting_sources["bank"] = "config"
                            initial_dirty.update(market_map)
                            print("[*] bankroll updated from dashboard config: %.2f USDT" % bank)
                    if args.poll is None:
                        configured_poll = float(runtime_cfg.get("poll", poll))
                        if math.isfinite(configured_poll) and configured_poll > 0:
                            poll = configured_poll
                            runtime_setting_sources["poll"] = "config"
                    if args.watch_sec is None:
                        configured_watch_sec = int(runtime_cfg.get("watch_sec", watch_sec))
                        if configured_watch_sec != watch_sec:
                            watch_sec = configured_watch_sec
                            runtime_setting_sources["watch_sec"] = "config"
                    if args.alert_gap is None:
                        configured_alert_gap = int(runtime_cfg.get("alert_gap", alert_gap))
                        if configured_alert_gap != alert_gap:
                            alert_gap = configured_alert_gap
                            runtime_setting_sources["alert_gap"] = "config"
                    try:
                        current_roi_revision = int(runtime_cfg.get("roi_revision", 0))
                    except (TypeError, ValueError):
                        current_roi_revision = roi_revision
                    if current_roi_revision != roi_revision:
                        configured_roi = float(runtime_cfg.get("min_roi_pct", min_roi * 100))
                        if args.min_roi is None:
                            new_min_roi = configured_roi / 100.0
                            if abs(new_min_roi - min_roi) >= 1e-9:
                                min_roi = new_min_roi
                                runtime_setting_sources["min_roi_pct"] = "config"
                                initial_dirty.update(market_map)
                                print("[*] minimum ROI updated from dashboard: %.2f%%"
                                      % configured_roi)
                        roi_revision = current_roi_revision
                    if router is not None:
                        wanted_sources = select_runtime_sources(
                            runtime_cfg.get("enabled_bk_sources", ["1win"]),
                            router.available_sources(),
                            no_bb=args.no_bb,
                        )
                        if set(wanted_sources) != set(router.active_sources()):
                            if router.configure_sources(wanted_sources):
                                initial_dirty.update(market_map)
                                best_by_slug.clear()
                                best_by_slug_updated_at.clear()
                                runtime_sources_changed = True
                                print("[*] bookmaker feeds updated from dashboard: %s"
                                      % (", ".join(wanted_sources) or "none"))
                except (TypeError, ValueError):
                    pass
            source_events = update_queue.drain(timeout=min(0.1, max(0.01, poll)))
            dirty_slugs = resolve_dirty_slugs(
                source_events, token_index, adapter_event_index
            )
            source_event_at = min(
                (event[2] for event in source_events if len(event) > 2),
                default=None,
            )
            source_event_at_by_source = {}
            for event in source_events:
                if len(event) < 3:
                    continue
                source = str(event[0])
                source_event_at_by_source[source] = min(
                    source_event_at_by_source.get(source, event[2]), event[2]
                )
            if initial_dirty:
                dirty_slugs.update(initial_dirty)
                initial_dirty.clear()
            if runtime_sources_changed:
                retry_bind()
                dirty_slugs.update(newly_bound_slugs)
                newly_bound_slugs.clear()
            snapshot_changed = False

            if args.watch and now - last_pm_refresh >= watch_sec:
                previous_market_map = dict(market_map)
                fresh, refresh_complete = fetch_live_pm_markets(with_status=True)
                had_previous_catalog = bool(market_map)
                market_map, added, removed = reconcile_pm_market_map(
                    market_map, fresh, refresh_complete
                )
                changed_slugs = {
                    slug for slug, market in market_map.items()
                    if slug not in previous_market_map or previous_market_map.get(slug) != market
                }
                removed_slugs = set(previous_market_map) - set(market_map)
                if removed_slugs:
                    for slug in removed_slugs:
                        removed_snapshot = live_snap.pop(slug, None)
                        if removed_snapshot is not None:
                            recent_signal_drops[slug] = _make_signal_drop_diagnostic(
                                removed_snapshot, "market_removed", time.time(), min_roi
                            )
                        best_by_slug.pop(slug, None)
                        best_by_slug_updated_at.pop(slug, None)
                    snapshot_changed = True
                dirty_slugs.update(changed_slugs)
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
                refresh_event_indexes()
                if changed_slugs:
                    retry_bind()
                    dirty_slugs.update(newly_bound_slugs)
                    newly_bound_slugs.clear()
                last_pm_refresh = time.time()

            if now - last_retry >= 10:
                retry_bind()
                dirty_slugs.update(newly_bound_slugs)
                newly_bound_slugs.clear()
                last_retry = time.time()

            # BetBoom remains a supported fallback; it has no event callback yet,
            # so only BetBoom-bound markets receive this slower refresh.
            if now - last_fallback_refresh >= 1.0:
                if router is None:
                    dirty_slugs.update(market_map)
                else:
                    dirty_slugs.update(
                        slug for slug in market_map if router.get_src(slug) == "BetBoom"
                    )
                last_fallback_refresh = now

            # Quotes can expire without a feed message; only active signals need a
            # one-second freshness sweep, not a full-market scan.
            if now - last_expiry_sweep >= 1.0:
                dirty_slugs.update(live_snap)
                last_expiry_sweep = now

            if dirty_slugs:
                scan_iteration += 1
            if now - last_phase_refresh >= 5.0:
                phase_counts_cache = {"live": 0, "prematch": 0, "unknown": 0}
                for _market in market_map.values():
                    _phase = classify_event_phase(_market)
                    phase_counts_cache[_phase] = phase_counts_cache.get(_phase, 0) + 1
                last_phase_refresh = now

            phase_counts = dict(phase_counts_cache)
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
                "invalid_candidate_pair": 0,
                "pm_depth_rejects": 0,
                "below_roi": 0,
                "roi_passed": 0,
            }
            source_counts = {}
            best_candidate_roi = None
            best_candidate_slug = None
            pm_no_ask_details = []
            for slug in sorted(dirty_slugs):
                m = market_map.get(slug)
                previous_snapshot = live_snap.pop(slug, None)
                if previous_snapshot is not None:
                    snapshot_changed = True
                best_by_slug.pop(slug, None)
                best_by_slug_updated_at.pop(slug, None)
                if m is None:
                    snapshot_changed = True
                    if previous_snapshot is not None:
                        recent_signal_drops[slug] = _make_signal_drop_diagnostic(
                            previous_snapshot, "market_removed", time.time(), min_roi
                        )
                    continue
                market_best_roi = None
                if router is not None:
                    live_bc = router.live_bc(slug)
                    _bk_src = router.get_src(slug) or "BetBoom"
                else:
                    live_bc = get_live_bc(slug)
                    _bk_src = "BetBoom"
                if not live_bc:
                    scan_counts["no_fresh_bookmaker"] += 1
                    if previous_snapshot is not None:
                        recent_signal_drops[slug] = _make_signal_drop_diagnostic(
                            previous_snapshot, "no_fresh_bookmaker_pair", time.time(), min_roi
                        )
                    continue
                scan_counts["with_fresh_bookmaker"] += 1
                source_counts[_bk_src] = source_counts.get(_bk_src, 0) + 1

                candidate_diagnostics = {}
                market_depth_rejects = 0
                market_invalid_calculations = 0
                plan_bank = _risk_capped_plan_bank(bank)
                pairs = build_arb_candidates(m, live_bc, slip, plan_bank, rate,
                                             bk_source=_bk_src,
                                             diagnostics=candidate_diagnostics,
                                             pm_diagnostics=pm_no_ask_details,
                                             market_slug=slug)
                current_pm_diagnostic = next(
                    (row for row in reversed(pm_no_ask_details)
                     if row.get("market_slug") == slug),
                    None,
                )
                for _reason, _count in candidate_diagnostics.items():
                    scan_counts[_reason] = scan_counts.get(_reason, 0) + _count
                scan_counts["candidate_pairs"] += len(pairs)
                for bets in pairs:
                    if not _is_pm_bookmaker_pair(
                        bets,
                        expected_bookmaker_source=_bk_src,
                        require_pm_token=True,
                        require_prices=True,
                    ):
                        scan_counts["invalid_candidate_pair"] += 1
                        continue
                    r = compute(bets, plan_bank)
                    if not r:
                        scan_counts["invalid_calculation"] += 1
                        market_invalid_calculations += 1
                        continue
                    r = refine_stakes(bets, r, rate)
                    if not r:
                        scan_counts["pm_depth_rejects"] += 1
                        market_depth_rejects += 1
                        continue
                    roi = r["roi"]
                    if best_candidate_roi is None or roi > best_candidate_roi:
                        best_candidate_roi = roi
                        best_candidate_slug = slug
                    if market_best_roi is None or roi > market_best_roi:
                        market_best_roi = roi
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
                        _display_stakes = [float(x) for x in r["stakes"]]
                        candidate_snapshot = {
                            "slug": slug,
                            "q": m.get("q", ""),
                            "sport": detect_sport(slug),
                            "market_type": detect_market_type(slug, m.get("q", "")),
                            "roi": float(roi),
                            "bank": sum(_display_stakes),
                            "total_bank": float(bank),
                            "currency": "USDT",
                            "rate": 1.0,
                            "bets": _serialize_candidate_bets(bets),
                            "stakes": _display_stakes,
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
                            "pm_event_url": f"https://polymarket.com/event/{slug}",
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
                        _pin_signal_snapshot(
                            pinned_signal_snapshots, candidate_snapshot,
                            now=candidate_snapshot["ts"],
                        )
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
                                pm_st = r["stakes"][i]
                            else:
                                bk_st = r["stakes"][i]
                        profit_upd = r["profit"]
                        prefix = "~" if changed else "="
                        print("  [%s] %s [%s] upd#%-3d  PM %.2f/%.2f USDT  %s %.2f/%.2f USDT  ROI %+.1f%% (was %+.1f%%)  profit +%.2f USDT" % (
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

                if slug in live_snap:
                    recent_signal_drops.pop(slug, None)
                elif previous_snapshot is not None:
                    reason = _classify_signal_drop_reason(
                        has_fresh_bookmaker=True,
                        candidate_diagnostics=candidate_diagnostics,
                        best_roi=market_best_roi,
                        min_roi=min_roi,
                        depth_rejects=market_depth_rejects,
                        invalid_calculations=market_invalid_calculations,
                        pm_diagnostic=current_pm_diagnostic,
                    )
                    recent_signal_drops[slug] = _make_signal_drop_diagnostic(
                        previous_snapshot, reason, time.time(), min_roi, market_best_roi,
                        current_pm_diagnostic,
                    )
                if market_best_roi is not None:
                    best_by_slug[slug] = market_best_roi
                    best_by_slug_updated_at[slug] = time.time()
                snapshot_changed = True

            batch_best_candidate_roi = best_candidate_roi
            batch_best_candidate_slug = best_candidate_slug
            _now = time.time()
            pm_book_states = {}
            for _detail in pm_no_ask_details:
                _state = _detail.get("book_state", "unknown")
                pm_book_states[_state] = pm_book_states.get(_state, 0) + 1
            if router is not None and _now - last_router_health_refresh >= 1.0:
                router_stats_cache = router.stats(market_map.keys())
                router_feed_health_cache = router.feed_health()
                last_router_health_refresh = _now
            matching_stats = None
            if router is not None:
                bound_markets = int(router_stats_cache.get("bindings", 0))
                matching_stats = {
                    "pm_markets": len(market_map),
                    "matched_pm_markets": bound_markets,
                    "unmatched_pm_markets": max(0, len(market_map) - bound_markets),
                    "bound_1win": int(router_stats_cache.get("bound_1win", 0)),
                    "bound_betboom": int(router_stats_cache.get("bound_betboom", 0)),
                    "onewin_catalog_matches": int(router_stats_cache.get("onewin_matches", 0)),
                    "fresh_1win_pairs": int(router_stats_cache.get("onewin_fresh_pairs", 0)),
                    "betboom_catalog_matches": int(router_stats_cache.get("bb_matches", 0)),
                }
            if _now - last_pm_diag_write >= 2.0:
                _write_json_atomic("pm_book_diagnostics.json", {
                    "ts": _now,
                    "cycle": scan_iteration,
                    "summary": {
                        "pm_no_ask_markets": len(pm_no_ask_details),
                        "by_book_state": pm_book_states,
                    },
                    "markets": pm_no_ask_details,
                })
                last_pm_diag_write = _now
            if _now - globals().get('_last_snap_write', 0) >= 2.0:
                _write_json_atomic("arbs_live.json", live_snap)
                globals()['_last_snap_write'] = _now

            if best_by_slug:
                best_candidate_slug, best_candidate_roi = max(
                    best_by_slug.items(), key=lambda item: item[1]
                )
                best_candidate_age_sec = round(max(
                    0.0, _now - best_by_slug_updated_at.get(best_candidate_slug, _now)
                ), 1)
            else:
                best_candidate_slug = None
                best_candidate_roi = None
                best_candidate_age_sec = None

            recent_signal_drop_rows = _prune_recent_signal_drops(
                recent_signal_drops, _now
            )
            pinned_signal_rows = _prune_pinned_signal_snapshots(
                pinned_signal_snapshots, _now
            )

            last_scan_status = {
                "state": "running",
                "ts": _now,
                "cycle": scan_iteration,
                "update_mode": "event",
                "checks_scope": "last_dispatch_batch",
                "processed_markets": len(dirty_slugs),
                "source_updates": len(source_events),
                "pending_updates": len(update_queue),
                "event_received_at": source_event_at,
                "event_to_snapshot_ms": round(max(0.0, _now - source_event_at) * 1000, 1)
                if source_event_at is not None else None,
                "source_event_to_snapshot_ms": {
                    source: round(max(0.0, _now - event_at) * 1000, 1)
                    for source, event_at in source_event_at_by_source.items()
                },
                "enabled_bk_sources": (
                    (["1win"] if router is not None and router.use_onewin else [])
                    + (["BetBoom"] if (router.use_bb if router is not None else BB_ENABLED) else [])
                ),
                "available_bk_sources": (
                    router.available_sources() if router is not None
                    else (["BetBoom"] if BB_ENABLED and not args.no_bb else [])
                ),
                "pm_markets": len(market_map),
                "phase_counts": phase_counts,
                "checks": scan_counts,
                "source_counts": source_counts,
                "matching_stats": matching_stats,
                "pm_book_summary": {
                    "pm_no_ask_markets": len(pm_no_ask_details),
                    "by_book_state": pm_book_states,
                },
                "bookmaker_feed_health": router_feed_health_cache,
                "pm_catalog_complete": pm_catalog_complete,
                "pm_catalog_retained": pm_catalog_retained,
                "pm_catalog_fetch": get_pm_catalog_diagnostics(),
                "signals": len(live_snap),
                "recent_signal_drops": recent_signal_drop_rows,
                "pinned_signal_snapshots": pinned_signal_rows,
                "min_roi_pct": round(min_roi * 100, 3),
                "best_candidate_roi_pct": round(best_candidate_roi * 100, 3)
                if best_candidate_roi is not None else None,
                "best_candidate_slug": best_candidate_slug,
                "best_candidate_roi_scope": (
                    "retained_market_cache" if best_candidate_slug is not None else None
                ),
                "best_candidate_age_sec": best_candidate_age_sec,
                "best_candidate_batch_roi_pct": round(batch_best_candidate_roi * 100, 3)
                if batch_best_candidate_roi is not None else None,
                "best_candidate_batch_slug": batch_best_candidate_slug,
                "best_candidate_batch_scope": "last_dispatch_batch",
                "bank_usdt": round(bank, 2),
                "currency": "USDT",
                "runtime_settings": {
                    "bank_usdt": round(bank, 2),
                    "min_roi_pct": round(min_roi * 100, 3),
                    "poll_sec": poll,
                    "watch_sec": watch_sec,
                    "alert_gap_sec": alert_gap,
                    "sources": dict(runtime_setting_sources),
                },
                "runtime_limits": runtime_limits,
                "config_warnings": list(CONFIG_WARNINGS) + list(runtime_limits.get("warnings", [])),
            }
            if _now - last_status_write >= 1.0:
                _write_scan_status(last_scan_status)
                last_status_write = _now
            if router is not None and dirty_slugs:
                refresh_event_indexes()
            if publisher is not None and (snapshot_changed or _now - last_publish >= 0.5):
                publisher.publish(live_snap, last_scan_status)
                last_publish = _now
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
        set_pm_update_callback(None)
        if publisher is not None:
            try:
                publisher.publish(live_snap, last_scan_status)
                publisher.close(timeout=1.0)
            except Exception:
                pass
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
