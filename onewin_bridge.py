# -*- coding: utf-8 -*-
"""Read-only 1win odds feed for Poly-ARB.

This module reads match catalog and odds only. It does not call bet placement,
slip, account, or wallet endpoints. It preserves the facade expected by
bk_router: get_global_stream(), bind_slug(), get_live_bc(), wait_ready(),
stop_global_stream().

The live path and message names mirror the sportsbook frontend bundle. Runtime
configuration can be overridden with ONEWIN_API_BASE, ONEWIN_ORIGIN,
ONEWIN_PARTNER_ID and ONEWIN_LANGUAGE. No location-spoofing header is sent.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import threading
import time
import unicodedata
from difflib import SequenceMatcher
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from urllib.parse import quote, urlencode, urlsplit, urlunsplit
from runtime_settings import resolve_onewin_odds_max_age

try:
    import requests
except ImportError:  # Keep imports/tests possible; start() reports the issue.
    requests = None

try:
    import websocket
except ImportError:
    websocket = None

API_BASE = os.getenv("ONEWIN_API_BASE", "https://api-gateway.top-parser.com").rstrip("/")
ORIGIN = os.getenv("ONEWIN_ORIGIN", "https://f1w-7707.com").rstrip("/")
# Public frontend partner identifier retained as a default; override per mirror.
PARTNER_ID = os.getenv("ONEWIN_PARTNER_ID", "44ba10e5-7df2-47ab-a44d-dc93803c7a6e")
LANGUAGE = os.getenv("ONEWIN_LANGUAGE", "ru-RU")
HTTP_MATCHES = API_BASE + "/matches/get-many"
# The frontend handles match-odds(-snapshot) on push-server-v2. The separate
# sportfeed-feature-ws channel is used for key-value updates, not match odds.
WS_PATH = "/push-server-v2/"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
WANTED_SPORTS = [142, 47, 37, 99, 33]
MAX_MATCHES_PER_SPORT = 200
# Subscribe to odds only after a PM market is successfully bound; avoid warming
# unrelated catalog events before the scanner knows which matches it needs.
INITIAL_SUBSCRIPTION_LIMIT = 0
SUBSCRIPTION_BATCH_SIZE = 30
MATCH_REFRESH_SECONDS = 180
MATCH_AMBIGUITY_MARGIN = 0.08
TIME_CORROBORATED_SPORT_IDS = {33}
TIME_CORROBORATED_MIN_SCORE = 0.60
TIME_CORROBORATED_MAX_DELTA_HOURS = 1.0
TEAM_ALIASES_FILE = os.getenv(
    "ONEWIN_TEAM_ALIASES_FILE",
    os.path.join(os.path.dirname(__file__), "team_aliases.json"),
)
ODDS_MAX_AGE_SECONDS, ODDS_MAX_AGE_SOURCE, ODDS_CONFIG_WARNINGS = (
    resolve_onewin_odds_max_age(os.getenv("ONEWIN_MAX_ODDS_AGE_SECONDS"))
)

PM_TO_ONEWIN_SPORT = {
    "cs2-": 142,
    "dota2-": 47,
    "lol-": 37,
    "valorant-": 99,
    "atp-": 33,
    "wta-": 33,
    "itf-": 33,
}
ONEWIN_SPORT_NAMES = {
    142: "CS2", 47: "DOTA2", 37: "LOL", 99: "VALORANT", 33: "TENNIS",
    136: "MOBILE_LEGENDS", 45: "R6", 170: "HEROES3",
}

_PM_MAP_RE = re.compile(r"-(?:game|map)(\d)(?=-|$)", re.I)
_PM_UNSUPPORTED = (
    "both-teams", "dragon", "baron", "kills-", "turret", "first-blood",
    "round-", "pistol", "exact-score", "odd-even", "odd_even",
    "map-total", "handicap-", "spread-", "-total-", "-ou-",
)
_PM_TOTAL_RE = re.compile(
    r"(?:^|-)(?:total|totals|over-under|overunder|ou)(?:-|$)", re.I
)
_PM_HANDICAP_RE = re.compile(r"(?:^|-)(?:handicap|spread)(?:-|$)", re.I)
_PM_SET_PERIOD_RE = re.compile(
    r"(?:^|-)(?:first-set|set(?:-\d+)?|sets|period(?:-\d+)?)(?:-|$)", re.I
)
_PM_PLAYER_PROP_RE = re.compile(
    r"(?:^|-)(?:any-player|player|ultra-kill|rampage|double-kill|"
    r"triple-kill|first-kill)(?:-|$)", re.I
)
_PM_EXPLICIT_WINNER_RE = re.compile(r"(?:^|-)(?:match-)?winner(?:-|$)", re.I)
_PM_CANONICAL_MATCH_RE = re.compile(r"-20\d{2}-\d{2}-\d{2}$")
_MAP_IN_GROUP_RE = re.compile(
    r"(?:\bmap\s*#?\s*(\d+)\b|\bкарта\s*#?\s*(\d+)\b|\b(\d+)\s*(?:st|nd|rd|th)?\s*(?:map|карта)\b)",
    re.I,
)
_WINNER_RE = re.compile(r"\b(?:winner|победитель|исход)\b", re.I)


def pm_market_kind(slug: str):
    s = (slug or "").strip().lower()
    if not s:
        return "unsupported", None, "empty_slug"
    if _PM_TOTAL_RE.search(s):
        return "unsupported", None, "total"
    if _PM_HANDICAP_RE.search(s):
        return "unsupported", None, "handicap"
    if _PM_SET_PERIOD_RE.search(s):
        return "unsupported", None, "set_period_not_supported"
    if _PM_PLAYER_PROP_RE.search(s) or any(x in s for x in _PM_UNSUPPORTED):
        return "unsupported", None, "special_or_player_prop"
    m = _PM_MAP_RE.search(s)
    if m:
        tail = s[m.end():]
        if not re.fullmatch(r"(?:-winner)?(?:-20\d{2}-\d{2}-\d{2})?", tail):
            return "unsupported", None, "unknown_period_market"
        return "map_winner", int(m.group(1)), None
    if _PM_EXPLICIT_WINNER_RE.search(s) or _PM_CANONICAL_MATCH_RE.search(s):
        return "winner", None, None
    return "unsupported", None, "unknown_market_signature"


def build_ws_url(api_base: str = API_BASE, language: str = LANGUAGE,
                 partner_id: str = PARTNER_ID) -> str:
    p = urlsplit(api_base)
    scheme = "wss" if p.scheme == "https" else "ws"
    query = urlencode({
        "Language": language,
        "externalPartnerId": partner_id,
        "EIO": "4",
        "transport": "websocket",
    })
    return urlunsplit((scheme, p.netloc, WS_PATH, query, ""))


def _build_event_url(match_id, slug, sport_type, sport_id, is_esport,
                     home_slug=None, away_slug=None):
    """Build only a normal match-page URL; never add a stake or bet payload."""
    parts = urlsplit(ORIGIN)
    if parts.scheme != "https" or not parts.netloc or parts.username or parts.password:
        return None
    mid = _key(match_id)
    raw_slug = str(slug or "").strip().strip("/")
    if not raw_slug and home_slug and away_slug:
        raw_slug = "%s-vs-%s" % (home_slug, away_slug)
    if not mid or not raw_slug or "/" in raw_slug or "\\" in raw_slug:
        return None
    if not raw_slug.endswith("-" + mid):
        raw_slug += "-" + mid
    route = str(sport_type or "").strip().lower()
    if not route:
        try:
            sport_num = int(sport_id or 0)
        except (TypeError, ValueError):
            sport_num = 0
        route = "esport" if is_esport or sport_num in {142, 47, 37, 99} else "sport"
    if not re.fullmatch(r"[a-z0-9-]+", route):
        return None
    base = urlunsplit((parts.scheme, parts.netloc, "", "", ""))
    return base + "/betting/match/" + quote(route, safe="-") + "/" + quote(raw_slug, safe="-")


def _key(value) -> str:
    return str(value) if value is not None else ""


def _finite_odds(value):
    try:
        n = float(value)
    except (TypeError, ValueError):
        return None
    return n if math.isfinite(n) and n > 1.0 else None


def _status_is_open(value) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"open", "active", "1"}
    try:
        return int(value) == 1
    except (TypeError, ValueError):
        return False


def _status_is_deleted(value) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"deleted", "2"}
    try:
        return int(value) == 2
    except (TypeError, ValueError):
        return False


def _group_market_kind(name: str):
    s = (name or "").strip().lower()
    if not s or any(x in s for x in ("total", "тотал", "handicap", "фора")):
        return "unsupported", None
    if not _WINNER_RE.search(s):
        return "unsupported", None
    m = _MAP_IN_GROUP_RE.search(s)
    if m:
        n = next((g for g in m.groups() if g), None)
        return ("map_winner", int(n)) if n else ("unsupported", None)
    return "winner", None


_CYRILLIC_TRANSLATION = str.maketrans({
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e",
    "ж": "zh", "з": "z", "и": "i", "й": "y", "к": "k", "л": "l",
    "м": "m", "н": "n", "о": "o", "п": "p", "р": "r", "с": "s",
    "т": "t", "у": "u", "ф": "f", "х": "kh", "ц": "ts", "ч": "ch",
    "ш": "sh", "щ": "shch", "ъ": "", "ы": "y", "ь": "", "э": "e",
    "ю": "yu", "я": "ya",
})
_GENERIC_TEAM_SUFFIXES = {"esport", "esports", "gaming"}


def _plain_name(value: str) -> str:
    """Fold punctuation, accents and common Cyrillic/Latin spelling variants."""
    s = unicodedata.normalize("NFKD", str(value or "").strip().casefold())
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.translate(_CYRILLIC_TRANSLATION).replace("&", " and ")
    tokens = re.findall(r"[a-z0-9]+", s)
    while tokens and tokens[-1] in _GENERIC_TEAM_SUFFIXES:
        tokens.pop()
    return " ".join(tokens)


def _build_alias_index(groups):
    index = {}
    for group_no, group in enumerate(groups or []):
        if not isinstance(group, (list, tuple)):
            continue
        normalized = {_plain_name(value) for value in group if _plain_name(value)}
        if len(normalized) < 2:
            continue
        key = "alias:%d" % group_no
        for name in normalized:
            if name in index and index[name] != key:
                index[name] = None
            elif name not in index:
                index[name] = key
    return index


def _load_alias_index(path=TEAM_ALIASES_FILE):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
        return _build_alias_index(payload.get("groups", [])) if isinstance(payload, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


_TEAM_ALIAS_INDEX = _load_alias_index()


def _parse_start_time(value):
    if value is None or value == "":
        return None
    try:
        if isinstance(value, (int, float)):
            stamp = float(value)
            if stamp > 10_000_000_000:
                stamp /= 1000.0
            return datetime.fromtimestamp(stamp, tz=timezone.utc)
        text = str(value).strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except (OverflowError, OSError, TypeError, ValueError):
        return None


def _start_delta_hours(pm_start, bk_start):
    pm_dt, bk_dt = _parse_start_time(pm_start), _parse_start_time(bk_start)
    if pm_dt is None or bk_dt is None:
        return None
    return abs((pm_dt - bk_dt).total_seconds()) / 3600.0


def _name_score(a: str, b: str) -> float:
    a, b = _plain_name(a), _plain_name(b)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    alias_a, alias_b = _TEAM_ALIAS_INDEX.get(a), _TEAM_ALIAS_INDEX.get(b)
    if alias_a and alias_a == alias_b:
        return 1.0
    tokens_a, tokens_b = set(a.split()), set(b.split())
    if tokens_a == tokens_b:
        return 0.98
    # A bare substring/team-name subset is too ambiguous (e.g. main vs academy).
    if tokens_a.issubset(tokens_b) or tokens_b.issubset(tokens_a):
        return 0.0
    compact_a, compact_b = a.replace(" ", ""), b.replace(" ", "")
    if min(len(compact_a), len(compact_b)) < 4:
        return 0.0

    def trigrams(value):
        padded = "  " + value + "  "
        return {padded[i:i + 3] for i in range(len(padded) - 2)}

    tri_a, tri_b = trigrams(a), trigrams(b)
    tri_score = len(tri_a & tri_b) / len(tri_a | tri_b) if tri_a or tri_b else 0.0
    token_score = len(tokens_a & tokens_b) / len(tokens_a | tokens_b)
    sequence_score = SequenceMatcher(None, a, b).ratio()
    return 0.50 * sequence_score + 0.30 * tri_score + 0.20 * token_score


def _team_from_outcome(odd: dict, match: dict):
    label = str(odd.get("name") or odd.get("outcome") or odd.get("shortName") or "").strip()
    low = _plain_name(label)
    if low in {"1", "home", "home team", "win1", "t1", "п1"}:
        return "home"
    if low in {"2", "away", "away team", "win2", "t2", "п2"}:
        return "away"
    home, away = match.get("home") or "", match.get("away") or ""
    hs, aw = _name_score(label, home), _name_score(label, away)
    if hs >= 0.76 and hs >= aw + 0.08:
        return "home"
    if aw >= 0.76 and aw >= hs + 0.08:
        return "away"
    return None


def _extract_items(data):
    if not isinstance(data, dict):
        return []
    candidates = [
        (data.get("result") or {}).get("items") if isinstance(data.get("result"), dict) else None,
        data.get("items"),
        (data.get("data") or {}).get("items") if isinstance(data.get("data"), dict) else None,
    ]
    return next((x for x in candidates if isinstance(x, list)), [])


class OneWinStream:
    def __init__(self, verbose: bool = False):
        self.verbose = verbose
        self.matches: dict[str, dict] = {}
        # match_id -> group_id -> {name, isBase, odds: {odd_id: odd}}
        self.stakes: dict[str, dict] = {}
        self.bindings: dict[str, dict] = {}
        self.match_diagnostics: dict[str, dict] = {}
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._connected = threading.Event()
        self._ws = None
        self._thread = None
        self._refresh_thread = None
        self._send_lock = threading.RLock()
        self.lock = threading.RLock()
        self._desired_ids: dict[str, object] = {}
        self._subscribed_info: set[str] = set()
        self._subscribed_odds: dict[bool, set[str]] = {True: set(), False: set()}
        self.last_error = None
        self._odds_snapshot_count = 0
        self._odds_delta_count = 0
        self._price_update_count = 0
        self._last_odds_message_at = None
        self._last_price_update_at = None
        self._odds_update_callback = None

    def set_odds_update_callback(self, callback):
        """Register a non-blocking listener called after an odds message is applied."""
        self._odds_update_callback = callback

    def _headers(self):
        return {
            "accept": "application/json",
            "content-type": "application/json",
            "x-external-partner-id": PARTNER_ID,
            "x-lang": LANGUAGE,
            "origin": ORIGIN,
            "referer": ORIGIN + "/",
            "user-agent": USER_AGENT,
        }

    def _fetch_sport(self, sport_id: int, limit: int):
        if requests is None:
            return None
        try:
            response = requests.post(
                HTTP_MATCHES,
                json={"sportId": sport_id, "limit": limit},
                headers=self._headers(),
                timeout=12,
            )
            if response.status_code != 200:
                self._log("catalog sport=%s HTTP %s", sport_id, response.status_code)
                return None
            return _extract_items(response.json())
        except Exception as exc:
            self._log("catalog sport=%s failed: %s", sport_id, type(exc).__name__)
            return None

    def fetch_live_matches(self, limit_per_sport: int = MAX_MATCHES_PER_SPORT):
        if requests is None:
            self.last_error = "missing dependency: requests"
            return []
        items = []
        successful_sports = set()
        with ThreadPoolExecutor(max_workers=min(5, len(WANTED_SPORTS))) as pool:
            futures = [pool.submit(self._fetch_sport, sid, limit_per_sport)
                       for sid in WANTED_SPORTS]
            for sid, future in zip(WANTED_SPORTS, futures):
                rows = future.result()
                if rows is None:
                    self._log("catalog %s: unavailable; retaining cached events",
                              ONEWIN_SPORT_NAMES.get(sid, sid))
                    continue
                successful_sports.add(sid)
                for row in rows:
                    if not isinstance(row, dict):
                        continue
                    if not row.get("sportId") and not row.get("sport_id"):
                        row = dict(row)
                        row["sportId"] = sid
                    items.append(row)
                self._log("catalog %s: %d events", ONEWIN_SPORT_NAMES.get(sid, sid), len(rows))
        unique = {}
        for m in items:
            if isinstance(m, dict) and m.get("id") is not None:
                unique[_key(m["id"])] = m
        if successful_sports:
            self._store_matches(list(unique.values()), replace_sport_ids=successful_sports)
            self.last_error = None if len(successful_sports) == len(WANTED_SPORTS) else "partial catalog fetch"
        return list(unique.values())

    def _store_matches(self, items, replace_sport_ids=None):
        items = list(items or [])
        incoming_ids = {
            _key(m.get("id")) for m in items
            if isinstance(m, dict) and m.get("id") is not None
        }
        with self.lock:
            if replace_sport_ids:
                replace_ids = {_key(sid) for sid in replace_sport_ids}
                for mid, old_match in list(self.matches.items()):
                    if (_key(old_match.get("sportId")) in replace_ids
                            and mid not in incoming_ids):
                        self.matches.pop(mid, None)
                        self.stakes.pop(mid, None)
            for m in items:
                if not isinstance(m, dict) or m.get("id") is None:
                    continue
                home = m.get("homeTeam") or m.get("home_team") or {}
                away = m.get("awayTeam") or m.get("away_team") or {}
                sport = m.get("sport") or {}
                category = m.get("category") or {}
                tournament = m.get("tournament") or {}
                sport_type = m.get("sportType") or sport.get("sportType")
                is_esport = m.get("isEsport", sport.get("isEsport", False))
                match_slug = m.get("slug")
                home_slug = home.get("slug") if isinstance(home, dict) else None
                away_slug = away.get("slug") if isinstance(away, dict) else None
                self.matches[_key(m["id"])] = {
                    "id": m["id"],
                    "name": m.get("name"),
                    "slug": match_slug,
                    "event_url": _build_event_url(
                        m["id"], match_slug, sport_type,
                        m.get("sportId") or m.get("sport_id") or sport.get("id"),
                        is_esport, home_slug, away_slug,
                    ),
                    "startAt": m.get("startAt") or m.get("start_at"),
                    "service": m.get("service"),
                    "serviceV2": m.get("serviceV2"),
                    "sportId": m.get("sportId") or m.get("sport_id") or sport.get("id"),
                    "sportTag": m.get("sportTag") or sport.get("tag"),
                    "sport_slug": sport.get("slug"),
                    "sport_type": sport_type,
                    "categoryId": m.get("categoryId") or category.get("id"),
                    "category_slug": category.get("slug"),
                    "tournamentId": m.get("tournamentId") or tournament.get("id"),
                    "tournament_slug": tournament.get("slug"),
                    "home": home.get("name") if isinstance(home, dict) else str(home or ""),
                    "away": away.get("name") if isinstance(away, dict) else str(away or ""),
                    "home_id": home.get("id") if isinstance(home, dict) else None,
                    "away_id": away.get("id") if isinstance(away, dict) else None,
                    "home_slug": home_slug,
                    "away_slug": away_slug,
                }
        self._ready.set()

    def start(self):
        if websocket is None or requests is None:
            raise RuntimeError("Install dependencies from requirements.txt")
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run_loop, name="onewin-feed", daemon=True)
        self._thread.start()
        self._refresh_thread = threading.Thread(
            target=self._refresh_loop, name="onewin-catalog-refresh", daemon=True
        )
        self._refresh_thread.start()

    def stop(self):
        self._stop.set()
        with self._send_lock:
            ws = self._ws
            self._ws = None
        if ws:
            try:
                ws.close()
            except Exception:
                pass
        self._connected.clear()

    def _refresh_loop(self):
        while not self._stop.wait(MATCH_REFRESH_SECONDS):
            self.fetch_live_matches()

    def _run_loop(self):
        attempt = 0
        while not self._stop.is_set():
            try:
                self.fetch_live_matches()
                self._set_initial_ids()
                if not self.matches:
                    self._stop.wait(min(30, 2 + attempt * 2))
                    attempt += 1
                    continue
                self._connect_and_loop()
                attempt = 0
            except Exception as exc:
                self.last_error = type(exc).__name__
                self._log("feed error: %s", type(exc).__name__)
            if not self._stop.is_set():
                delay = min(20, 2 * (1.6 ** min(attempt, 5)))
                self._stop.wait(delay)
                attempt += 1

    def _set_initial_ids(self):
        with self.lock:
            for mid, m in list(self.matches.items())[:INITIAL_SUBSCRIPTION_LIMIT]:
                self._desired_ids[mid] = m["id"]

    def _connect_and_loop(self):
        if websocket is None:
            raise RuntimeError("missing dependency: websocket-client")
        url = build_ws_url()
        self._log("connecting to sportsbook odds feed at %s%s", urlsplit(API_BASE).netloc, WS_PATH)
        ws = websocket.create_connection(
            url,
            timeout=15,
            header=["User-Agent: " + USER_AGENT, "Referer: " + ORIGIN + "/"],
            origin=ORIGIN,
        )
        with self._send_lock:
            self._ws = ws
        self._subscribed_info.clear()
        self._subscribed_odds = {True: set(), False: set()}
        try:
            greeting = ws.recv()
            if not isinstance(greeting, str) or not greeting.startswith("0"):
                raise RuntimeError("unexpected Engine.IO greeting")
            ws.send("40")
            ws.settimeout(2.0)
            deadline = time.monotonic() + 8.0
            connected = False
            while not self._stop.is_set() and time.monotonic() < deadline:
                try:
                    msg = ws.recv()
                except websocket.WebSocketTimeoutException:
                    continue
                if msg == "2":
                    ws.send("3")
                elif isinstance(msg, str) and msg.startswith("40"):
                    connected = True
                    break
                elif isinstance(msg, str) and msg.startswith("41"):
                    raise RuntimeError("feed namespace rejected")
                elif isinstance(msg, str) and msg.startswith("42"):
                    self._handle_socket_message(msg)
            if not connected:
                raise RuntimeError("feed connect acknowledgement timed out")
            # Never expose prices from a prior socket session while waiting
            # for the new match-odds snapshots.
            with self.lock:
                self.stakes.clear()
            self._connected.set()
            self._ready.set()
            with self.lock:
                ids = list(self._desired_ids.values())
            self._subscribe_ids(ws, ids)
            while not self._stop.is_set():
                try:
                    msg = ws.recv()
                except websocket.WebSocketTimeoutException:
                    continue
                if not isinstance(msg, str) or not msg:
                    continue
                if msg == "2":
                    ws.send("3")
                elif msg.startswith("41"):
                    break
                elif msg.startswith("42"):
                    self._handle_socket_message(msg)
        finally:
            self._connected.clear()
            with self._send_lock:
                if self._ws is ws:
                    self._ws = None
            try:
                ws.close()
            except Exception:
                pass

    def _send_packet(self, ws, message_type: str, ids, is_base=None):
        data = {"matchIds": list(ids)}
        if is_base is not None:
            data["isBaseOddsGroups"] = bool(is_base)
        packet = ["subscribe", {"messageType": message_type, "data": data}]
        ws.send("42" + json.dumps(packet, separators=(",", ":")))

    def _subscribe_ids(self, ws, ids):
        unique = {}
        for mid in ids:
            if mid is not None:
                unique[_key(mid)] = mid
        values = list(unique.values())
        if not values:
            return
        for start in range(0, len(values), SUBSCRIPTION_BATCH_SIZE):
            batch = values[start:start + SUBSCRIPTION_BATCH_SIZE]
            keys = {_key(mid) for mid in batch}
            with self._send_lock:
                if self._ws is not ws or not self._connected.is_set():
                    return
                info_new = [mid for mid in batch if _key(mid) not in self._subscribed_info]
                if info_new:
                    self._send_packet(ws, "subscribe-match-info", info_new)
                    self._subscribed_info.update(_key(mid) for mid in info_new)
                for base in (True, False):
                    odds_new = [mid for mid in batch if _key(mid) not in self._subscribed_odds[base]]
                    if odds_new:
                        self._send_packet(ws, "subscribe-match-odds", odds_new, base)
                        self._subscribed_odds[base].update(_key(mid) for mid in odds_new)
            self._log("subscribed batch: %d matches; odds groups base/all", len(batch))

    def _subscribe_match(self, match_id):
        if match_id is None:
            return
        key = _key(match_id)
        with self.lock:
            self._desired_ids[key] = match_id
        ws = self._ws
        if ws and self._connected.is_set():
            self._subscribe_ids(ws, [match_id])

    def _handle_socket_message(self, msg: str):
        try:
            arr = json.loads(msg[2:])
        except (TypeError, ValueError):
            return
        if not isinstance(arr, list) or len(arr) < 2 or arr[0] != "u":
            return
        payload = arr[1]
        if not isinstance(payload, dict):
            return
        message_type = payload.get("messageType")
        data = payload.get("data") or {}
        if not isinstance(data, dict):
            return
        if message_type in ("match-info", "match-info-snapshot"):
            self._on_match_info(data)
        elif message_type == "match-odds-snapshot":
            self._on_match_odds(data, snapshot=True)
        elif message_type == "match-odds":
            self._on_match_odds(data, snapshot=False)

    def _on_match_info(self, data: dict):
        mid = data.get("matchId")
        if mid is None:
            return
        with self.lock:
            match = self.matches.setdefault(_key(mid), {"id": mid})
            match["service"] = data.get("service") or match.get("service")
            match["enabledOddsCount"] = data.get("enabledOddsCount")
            match["scoreBoard"] = data.get("scoreBoard")

    def _on_match_odds(self, data: dict, snapshot: bool = False):
        mid = data.get("matchId")
        if mid is None:
            return
        match_key = _key(mid)
        base_filter = data.get("isBaseOddsGroups")
        now = time.time()
        with self.lock:
            if snapshot:
                self._odds_snapshot_count += 1
            else:
                self._odds_delta_count += 1
            self._last_odds_message_at = now
            bucket = self.stakes.setdefault(match_key, {})
            if snapshot and base_filter is False:
                # The non-base subscription carries the complete odds-group
                # snapshot, so remove groups that disappeared since reconnect.
                bucket.clear()
            elif snapshot and base_filter is True:
                for gid, old_group in list(bucket.items()):
                    if old_group.get("isBase") is True:
                        bucket.pop(gid, None)
            for group in data.get("oddsGroups", []) or []:
                if not isinstance(group, dict):
                    continue
                if snapshot and base_filter is True and group.get("isBase") is False:
                    continue
                group_id = _key(group.get("id") or group.get("name"))
                if not group_id:
                    continue
                old = bucket.get(group_id, {})
                odds = {} if snapshot else dict(old.get("odds") or {})
                for odd in group.get("oddsList", []) or []:
                    if not isinstance(odd, dict):
                        continue
                    odd_id = _key(odd.get("id"))
                    if not odd_id:
                        continue
                    if _status_is_deleted(odd.get("status")):
                        odds.pop(odd_id, None)
                        continue
                    merged = dict(odds.get(odd_id) or {})
                    old_received_at = merged.get("receivedAt")
                    merged.update(odd)
                    if _finite_odds(odd.get("cf")) is not None:
                        merged["receivedAt"] = now
                        self._price_update_count += 1
                        self._last_price_update_at = now
                    elif old_received_at is not None:
                        # A status/name-only delta does not reaffirm the cached price.
                        merged["receivedAt"] = old_received_at
                    else:
                        merged.pop("receivedAt", None)
                    odds[odd_id] = merged
                merged_group = dict(old)
                for field in ("id", "name", "isBase", "renderType", "period", "periodName"):
                    if field in group:
                        merged_group[field] = group[field]
                merged_group.update({"groupId": group_id, "odds": odds, "receivedAt": now})
                bucket[group_id] = merged_group
        callback = self._odds_update_callback
        if callback is not None:
            try:
                callback(mid)
            except Exception:
                pass

    def _pm_sport_id(self, slug: str):
        s = (slug or "").lower()
        return next((sid for pref, sid in PM_TO_ONEWIN_SPORT.items() if s.startswith(pref)), None)

    def _record_match_diagnostic(self, slug, payload):
        with self.lock:
            self.match_diagnostics[str(slug)] = payload

    def snapshot_match_diagnostics(self):
        with self.lock:
            return {slug: dict(value) for slug, value in self.match_diagnostics.items()}

    def snapshot_health(self):
        now = time.time()
        with self.lock:
            matches_count = len(self.matches)
            bindings_count = len(self.bindings)
            snapshot_count = self._odds_snapshot_count
            delta_count = self._odds_delta_count
            price_update_count = self._price_update_count
            last_message_at = self._last_odds_message_at
            last_price_at = self._last_price_update_at
            fresh_open_quotes = 0
            stale_open_quotes = 0
            for groups in self.stakes.values():
                for group in groups.values():
                    for odd in (group.get("odds") or {}).values():
                        if not _status_is_open(odd.get("status")):
                            continue
                        if _finite_odds(odd.get("cf")) is None:
                            continue
                        try:
                            age_sec = now - float(odd.get("receivedAt"))
                        except (TypeError, ValueError):
                            age_sec = float("inf")
                        if 0.0 <= age_sec <= ODDS_MAX_AGE_SECONDS:
                            fresh_open_quotes += 1
                        else:
                            stale_open_quotes += 1
        with self._send_lock:
            subscribed_matches = len(self._subscribed_odds.get(False, set()))
        return {
            "connected": self._connected.is_set(),
            "catalog_ready": self._ready.is_set() and matches_count > 0,
            "catalog_matches": matches_count,
            "bound_pm_markets": bindings_count,
            "subscribed_matches": subscribed_matches,
            "fresh_open_quotes": fresh_open_quotes,
            "stale_open_quotes": stale_open_quotes,
            "odds_snapshots_received": snapshot_count,
            "odds_deltas_received": delta_count,
            "valid_price_updates_received": price_update_count,
            "last_odds_message_age_sec": (
                round(max(0.0, now - last_message_at), 1)
                if last_message_at is not None else None
            ),
            "last_price_update_age_sec": (
                round(max(0.0, now - last_price_at), 1)
                if last_price_at is not None else None
            ),
            "max_quote_age_sec": ODDS_MAX_AGE_SECONDS,
            "last_error": self.last_error,
        }

    def bind(self, slug: str, pm_teams, pm_context=None):
        kind, arg, unsupported_subtype = pm_market_kind(slug)
        teams_ok = isinstance(pm_teams, (list, tuple)) and len(pm_teams) == 2
        base = {
            "slug": str(slug),
            "pm_teams": list(pm_teams) if teams_ok else [],
            "market_kind": kind,
        }

        def record(reason, rows=None, threshold=None, selected=None, **extra):
            top = []
            for score, direct, reverse, delta_hours, candidate in (rows or [])[:3]:
                top.append({
                    "match_id": candidate.get("id"),
                    "home": candidate.get("home"),
                    "away": candidate.get("away"),
                    "score": round(score, 4),
                    "direct_score": round(direct, 4),
                    "reverse_score": round(reverse, 4),
                    "start_delta_hours": round(delta_hours, 2) if delta_hours is not None else None,
                })
            payload = dict(base)
            payload.update({"reason": reason, "threshold": threshold, "top_candidates": top})
            if selected is not None:
                payload["selected_match_id"] = selected.get("id")
            payload.update(extra)
            self._record_match_diagnostic(slug, payload)

        if kind not in ("winner", "map_winner"):
            record("unsupported_market_type", unsupported_subtype=unsupported_subtype)
            return None
        if not teams_ok:
            record("invalid_team_outcomes")
            return None
        sport_id = self._pm_sport_id(slug)
        if sport_id is None:
            record("unsupported_sport")
            return None
        base["sport_id"] = sport_id
        threshold = 0.65 if sport_id == 33 else 0.75
        pm_context = pm_context if isinstance(pm_context, dict) else {}
        pm_start = (pm_context.get("start") or pm_context.get("eventStartTime")
                    or pm_context.get("startAt"))
        with self.lock:
            candidates = list(self.matches.values())
        scored = []
        for match in candidates:
            try:
                if int(match.get("sportId")) != sport_id:
                    continue
            except (TypeError, ValueError):
                continue
            home, away = match.get("home"), match.get("away")
            if not home or not away:
                continue
            direct_parts = (_name_score(pm_teams[0], home), _name_score(pm_teams[1], away))
            reverse_parts = (_name_score(pm_teams[0], away), _name_score(pm_teams[1], home))
            direct = min(direct_parts)
            reverse = min(reverse_parts)
            score = max(direct, reverse)
            delta_hours = _start_delta_hours(pm_start, match.get("startAt"))
            scored.append((score, direct, reverse, delta_hours, match))
        scored.sort(key=lambda item: item[0], reverse=True)
        if not scored:
            record("no_sport_candidates", threshold=threshold)
            return None
        best_score, _, _, _, best = scored[0]
        used_time_corroboration = False
        if not best or best_score < threshold:
            corroborated = []
            if sport_id in TIME_CORROBORATED_SPORT_IDS:
                corroborated = [
                    row for row in scored
                    if row[0] >= TIME_CORROBORATED_MIN_SCORE
                    and row[3] is not None
                    and row[3] <= TIME_CORROBORATED_MAX_DELTA_HOURS
                ]
            if len(corroborated) == 1:
                best_score, _, _, _, best = corroborated[0]
                used_time_corroboration = True
            else:
                reason = "ambiguous_time_corroboration" if len(corroborated) > 1 else "below_threshold"
                record(
                    reason, rows=scored, threshold=threshold,
                    time_corroboration_candidates=len(corroborated),
                    time_corroboration_min_score=TIME_CORROBORATED_MIN_SCORE,
                    time_corroboration_max_delta_hours=TIME_CORROBORATED_MAX_DELTA_HOURS,
                )
                return None
        near_best = [row for row in scored
                     if row[0] >= threshold and best_score - row[0] < MATCH_AMBIGUITY_MARGIN]
        used_time_context = False
        if len(near_best) > 1:
            with_time = [row for row in near_best if row[3] is not None]
            if len(with_time) == len(near_best):
                by_time = sorted(with_time, key=lambda row: row[3])
                if by_time[0][3] <= 24.0 and by_time[1][3] - by_time[0][3] >= 6.0:
                    best_score, _, _, _, best = by_time[0]
                    used_time_context = True
                else:
                    record("ambiguous_event", rows=scored, threshold=threshold)
                    return None
            else:
                record("ambiguous_event", rows=scored, threshold=threshold)
                return None
        direct_avg = (_name_score(pm_teams[0], best["home"]) + _name_score(pm_teams[1], best["away"])) / 2
        reverse_avg = (_name_score(pm_teams[0], best["away"]) + _name_score(pm_teams[1], best["home"])) / 2
        event_key = "%s:%s" % (
            sport_id,
            "|".join(sorted((_plain_name(pm_teams[0]), _plain_name(pm_teams[1])))),
        )
        binding = {
            "match_id": best["id"], "sport_id": sport_id, "kind": kind,
            "arg": arg, "score": best_score, "swap": reverse_avg > direct_avg,
            "home": best["home"], "away": best["away"],
            "pm_home": pm_teams[0], "pm_away": pm_teams[1], "bound_at": time.time(),
            "event_url": best.get("event_url"), "event_key": event_key,
            "start_delta_hours": _start_delta_hours(pm_start, best.get("startAt")),
        }
        with self.lock:
            self.bindings[slug] = binding
        record(
            ("matched_time_corroborated" if used_time_corroboration else
             "matched_time_context" if used_time_context else "matched"),
            rows=scored, threshold=threshold, selected=best,
            event_key=event_key,
            time_corroborated=used_time_corroboration,
        )
        self._subscribe_match(best["id"])
        return dict(binding)

    def live_bc(self, slug: str):
        if not self._connected.is_set():
            return {}
        with self.lock:
            binding = self.bindings.get(slug)
            if not binding:
                return {}
            match = self.matches.get(_key(binding["match_id"]), {})
            groups = list((self.stakes.get(_key(binding["match_id"])) or {}).values())
        if not match.get("home") or not match.get("away"):
            return {}
        matching = []
        for group in groups:
            group_kind, group_arg = _group_market_kind(group.get("name"))
            if group_kind == binding["kind"] and group_arg == binding.get("arg"):
                matching.append(group)
        # Multiple identically classified groups are ambiguous; don't guess.
        if len(matching) != 1:
            return {}
        candidates = {"home": [], "away": []}
        now = time.time()
        for odd in (matching[0].get("odds") or {}).values():
            if not _status_is_open(odd.get("status")):
                continue
            price = _finite_odds(odd.get("cf"))
            if price is None:
                continue
            try:
                age_sec = now - float(odd.get("receivedAt"))
            except (TypeError, ValueError):
                continue
            if not math.isfinite(age_sec) or age_sec < 0.0 or age_sec > ODDS_MAX_AGE_SECONDS:
                continue
            side = _team_from_outcome(odd, match)
            if side:
                candidates[side].append((price, age_sec))
        if len(candidates["home"]) != 1 or len(candidates["away"]) != 1:
            return {}
        sides = {"home": candidates["home"][0][0], "away": candidates["away"][0][0]}
        ages = {"home": candidates["home"][0][1], "away": candidates["away"][0][1]}
        if binding["swap"]:
            sides = {"home": sides["away"], "away": sides["home"]}
            ages = {"home": ages["away"], "away": ages["home"]}
        # Preserve full precision for downstream TTL checks; round only in UI.
        return {
            binding["pm_home"]: {
                "src": "1win", "o": sides["home"], "c": 0.0,
                "age_sec": ages["home"],
            },
            binding["pm_away"]: {
                "src": "1win", "o": sides["away"], "c": 0.0,
                "age_sec": ages["away"],
            },
        }

    def snapshot_matches(self):
        with self.lock:
            return list(self.matches.values())

    def is_ready(self):
        return self._ready.is_set() and self._connected.is_set() and bool(self.matches)

    def _log(self, fmt: str, *args):
        if self.verbose:
            print("[1WIN] " + (fmt % args), flush=True)


_GLOBAL = None
_GLOBAL_LOCK = threading.Lock()


def get_global_stream(verbose: bool = False):
    global _GLOBAL
    with _GLOBAL_LOCK:
        if _GLOBAL is None:
            _GLOBAL = OneWinStream(verbose=verbose)
            _GLOBAL.start()
        elif verbose:
            _GLOBAL.verbose = True
        return _GLOBAL


def stop_global_stream():
    global _GLOBAL
    with _GLOBAL_LOCK:
        stream = _GLOBAL
        _GLOBAL = None
    if stream is not None:
        stream.stop()


def wait_ready(timeout: float = 120, min_matches: int = 1, poll: float = 0.5) -> bool:
    stream = get_global_stream()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if stream.is_ready() and len(stream.matches) >= min_matches:
            return True
        time.sleep(poll)
    return False


def bind_slug(slug: str, pm_teams, pm_context=None):
    try:
        return get_global_stream().bind(slug, pm_teams, pm_context)
    except Exception:
        return None


def get_binding(slug: str):
    stream = get_global_stream()
    with stream.lock:
        binding = stream.bindings.get(slug)
        return dict(binding) if binding else None


def get_match_diagnostics():
    return get_global_stream().snapshot_match_diagnostics()


def get_live_bc(slug: str):
    try:
        return get_global_stream().live_bc(slug)
    except Exception:
        return {}


def smoke_test(verbose: bool = False, seconds: float = 15):
    print("[1WIN] READ-ONLY feed smoke test (no bet/slip requests)", flush=True)
    stream = get_global_stream(verbose=verbose)
    try:
        if not wait_ready(timeout=45, min_matches=1):
            print("[1WIN] no matches received; check normal site access/config", flush=True)
            return 1
        time.sleep(max(0, seconds))
        with stream.lock:
            match_count = len(stream.matches)
            group_count = sum(len(groups) for groups in stream.stakes.values())
            odds_count = sum(len(group.get("odds") or {})
                             for groups in stream.stakes.values() for group in groups.values())
            samples = []
            for mid, groups in stream.stakes.items():
                match = stream.matches.get(mid) or {}
                for group in groups.values():
                    open_odds = [o for o in (group.get("odds") or {}).values()
                                 if _status_is_open(o.get("status")) and _finite_odds(o.get("cf"))]
                    if open_odds:
                        samples.append({
                            "event": "%s — %s" % (match.get("home") or "?", match.get("away") or "?"),
                            "market": group.get("name") or "?",
                            "odds": [
                                {"outcome": o.get("name") or o.get("outcome"), "cf": o.get("cf")}
                                for o in open_odds[:4]
                            ],
                        })
                    if len(samples) >= 3:
                        break
                if len(samples) >= 3:
                    break
        print("[1WIN] connected=%s matches=%d groups=%d open_odds=%d" % (
            stream._connected.is_set(), match_count, group_count, odds_count), flush=True)
        for sample in samples:
            print("[1WIN] SAMPLE " + json.dumps(sample, ensure_ascii=False), flush=True)
        return 0 if odds_count else 2
    finally:
        stop_global_stream()


def main():
    parser = argparse.ArgumentParser(description="Read-only 1win odds feed smoke test")
    parser.add_argument("--verbose", "-v", action="store_true")
    parser.add_argument("--smoke-seconds", type=float, default=15)
    args = parser.parse_args()
    raise SystemExit(smoke_test(verbose=args.verbose, seconds=args.smoke_seconds))


if __name__ == "__main__":
    main()
