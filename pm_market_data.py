"""Polymarket CLOB market-data feed, order book, and bounded REST recovery.

The scanner facade passes legacy callback/executor seams explicitly so event
ordering and existing deterministic tests remain compatible.
"""
import json
import math
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import requests
from runtime_settings import RUNTIME_LIMIT_DEFAULTS
from pm_fees import fee_per_share as pm_fee_per_share

WS_PING_INTERVAL_SEC = 8.0
WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
CLOB = "https://clob.polymarket.com"
HAS_WS = False
websocket = None
BOOKS = {}
LOCK = threading.Lock()
STOP = threading.Event()
_PM_UPDATE_CALLBACK = None

def _notify_pm_update(token_id):
    callback = _PM_UPDATE_CALLBACK
    if callback is not None:
        try: callback(str(token_id))
        except Exception: pass

def _parse_clob_event_timestamp(value):
    """Normalize CLOB event timestamps to Unix seconds; return None if unknown."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, datetime):
        parsed = value
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        timestamp = parsed.timestamp()
    else:
        try:
            timestamp = float(value)
        except (TypeError, ValueError):
            if not isinstance(value, str):
                return None
            try:
                parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                timestamp = parsed.timestamp()
            except (TypeError, ValueError, OverflowError):
                return None
    if not math.isfinite(timestamp) or timestamp <= 0:
        return None
    if timestamp >= 100_000_000_000:
        timestamp /= 1000.0
    return timestamp


def _book_source_timestamp(book):
    if not isinstance(book, dict):
        return None
    try:
        timestamp = float(book.get("source_ts"))
    except (TypeError, ValueError):
        return None
    return timestamp if math.isfinite(timestamp) and timestamp > 0 else None


def _parse_reported_top_price(value):
    if value is None or (isinstance(value, str) and not value.strip()):
        return True, None
    try:
        price = float(value)
    except (TypeError, ValueError):
        return False, None
    if not math.isfinite(price):
        return False, None
    if price == 0:
        return True, None
    if 0 < price < 1:
        return True, price
    return False, None


def _reported_tops_match(bids, asks, reported):
    for field, levels in (("best_bid", bids), ("best_ask", asks)):
        if field not in reported:
            continue
        local = levels[0][0] if levels else None
        remote = reported[field]
        if remote is None:
            if local is not None:
                return False
        elif local is None or not math.isclose(local, remote, rel_tol=0.0, abs_tol=1e-9):
            return False
    return True


class Feed(threading.Thread):
    def __init__(self, ids, on_update=None, schedule_refresh=None):
        super().__init__(daemon=True)
        self.ids = sorted({str(x) for x in ids if x})
        self._ids_lock = threading.RLock()
        self._generation = 0
        self._wake = threading.Event()
        self._on_update = on_update or _notify_pm_update
        self._schedule_refresh = schedule_refresh or _schedule_pm_book_refresh

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
                last_ping = time.monotonic()
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
                    now_mono = time.monotonic()
                    if now_mono - last_ping >= WS_PING_INTERVAL_SEC:
                        try:
                            ws.send("PING")
                        except Exception:
                            break
                        last_ping = now_mono
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
        if not isinstance(d, dict):
            return
        et = d.get("event_type")
        if et == "book":
            aid = d.get("asset_id")
            if not aid:
                return
            aid = str(aid)
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
            event_ts = _parse_clob_event_timestamp(d.get("timestamp"))
            with LOCK:
                prev = BOOKS.get(aid)
                prev_source_ts = _book_source_timestamp(prev)
                if (event_ts is not None and prev_source_ts is not None
                        and event_ts < prev_source_ts):
                    return
                prev_asks = (prev or {}).get("asks") or []
                prev_ask = prev_asks[0][0] if prev_asks else None
                cur_ask = asks[0][0] if asks else None
                if prev is None or prev_ask != cur_ask:
                    last_change = now
                else:
                    last_change = prev.get("last_change", now)
                BOOKS[aid] = {
                    "bids": bids, "asks": asks, "ts": now,
                    "last_change": last_change,
                    "source_ts": event_ts if event_ts is not None else prev_source_ts,
                }
            self._on_update(aid)
            return
        if et == "price_change":
            pcs = d.get("price_changes") or d.get("changes") or []
            if isinstance(pcs, dict):
                pcs = [pcs]
            if not isinstance(pcs, (list, tuple)):
                return
            event_ts = _parse_clob_event_timestamp(d.get("timestamp"))
            now = time.time()
            changes_by_id = {}
            invalid_ids = set()
            reported_tops = {}
            for ch in pcs:
                if not isinstance(ch, dict):
                    continue
                aid = ch.get("asset_id")
                if not aid:
                    continue
                aid = str(aid)
                try:
                    side = str(ch.get("side") or "").upper()
                    price = float(ch.get("price", 0))
                    size = float(ch.get("size", 0))
                except (TypeError, ValueError):
                    invalid_ids.add(aid)
                    continue
                if (not math.isfinite(price) or not math.isfinite(size)
                        or not 0 < price < 1 or size < 0
                        or side not in {"BUY", "SELL"}):
                    invalid_ids.add(aid)
                    continue
                changes_by_id.setdefault(aid, []).append((side, price, size))
                tops = {}
                for field in ("best_bid", "best_ask"):
                    if field in ch:
                        valid, top_price = _parse_reported_top_price(ch.get(field))
                        if not valid:
                            invalid_ids.add(aid)
                        else:
                            tops[field] = top_price
                # The last change's top-of-book values describe the final batch state.
                reported_tops[aid] = tops

            updated_ids = set()
            invalidated_ids = set()
            refresh_reasons = {}
            with LOCK:
                all_ids = set(changes_by_id) | invalid_ids
                for aid in sorted(all_ids):
                    if aid in invalid_ids:
                        BOOKS.pop(aid, None)
                        invalidated_ids.add(aid)
                        refresh_reasons[aid] = "invalid_delta"
                        continue
                    prev = BOOKS.get(aid)
                    if not prev:
                        refresh_reasons[aid] = "missing_snapshot"
                        continue
                    prev_source_ts = _book_source_timestamp(prev)
                    if (event_ts is not None and prev_source_ts is not None
                            and event_ts < prev_source_ts):
                        continue
                    try:
                        bids = {float(p): float(s) for p, s in prev.get("bids", [])}
                        asks = {float(p): float(s) for p, s in prev.get("asks", [])}
                    except (TypeError, ValueError):
                        BOOKS.pop(aid, None)
                        invalidated_ids.add(aid)
                        refresh_reasons[aid] = "invalid_snapshot"
                        continue
                    if any(not math.isfinite(p) or not math.isfinite(s)
                           or not 0 < p < 1 or s <= 0 for p, s in list(bids.items()) + list(asks.items())):
                        BOOKS.pop(aid, None)
                        invalidated_ids.add(aid)
                        refresh_reasons[aid] = "invalid_snapshot"
                        continue
                    for side, price, size in changes_by_id[aid]:
                        book_side = bids if side == "BUY" else asks
                        if size == 0:
                            book_side.pop(price, None)
                        else:
                            book_side[price] = size
                    new_bids = sorted(bids.items(), key=lambda z: -z[0])
                    new_asks = sorted(asks.items(), key=lambda z: z[0])
                    if not _reported_tops_match(
                            new_bids, new_asks, reported_tops.get(aid, {})):
                        BOOKS.pop(aid, None)
                        invalidated_ids.add(aid)
                        refresh_reasons[aid] = "top_of_book_mismatch"
                        continue
                    prev_asks = prev.get("asks") or []
                    prev_ask = prev_asks[0][0] if prev_asks else None
                    cur_ask = new_asks[0][0] if new_asks else None
                    if prev_ask != cur_ask:
                        last_change = now
                    else:
                        last_change = prev.get("last_change", now)
                    BOOKS[aid] = {
                        "bids": new_bids, "asks": new_asks, "ts": now,
                        "last_change": last_change,
                        "source_ts": event_ts if event_ts is not None else prev_source_ts,
                    }
                    updated_ids.add(aid)
            for aid in updated_ids | invalidated_ids:
                self._on_update(aid)
            for aid, reason in refresh_reasons.items():
                self._schedule_refresh(aid, reason)
            return
        if et == "last_trade_price":
            # A completed trade is not evidence that cached resting bids/asks
            # are current. Keep order-book TTL anchored only to book snapshots
            # and price-level deltas, and do not trigger a quote recalculation.
            return



BOOK_TTL = RUNTIME_LIMIT_DEFAULTS["pm_book_ttl_sec"]
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


def walk_book_with_fee(aid, target_cost, rate, exponent):
    """Walk asks by total cash cost, including per-market taker fees.

    The CLOB formula is shares * rate * (p * (1-p)) ** exponent. Rounding and
    minimum-fee behavior are left as execution-time uncertainty; this is a
    continuous, read-only estimate for scanner ranking.
    """
    try:
        target_cost = float(target_cost)
        rate = float(rate)
        exponent = float(exponent)
    except (TypeError, ValueError, OverflowError):
        return None
    if (not all(math.isfinite(x) for x in (target_cost, rate, exponent))
            or target_cost <= 0.0 or rate < 0.0 or exponent <= 0.0):
        return None
    with LOCK:
        book = BOOKS.get(aid)
        if not book:
            return None
        ts = book.get("ts", 0.0)
        if ts <= 0 or (time.time() - ts) > BOOK_TTL:
            return None
        levels = list(book.get("asks") or [])
    remaining_cost = target_cost
    shares = gross_cost = fee_total = 0.0
    for price, available_shares in levels:
        try:
            price, available_shares = float(price), float(available_shares)
        except (TypeError, ValueError, OverflowError):
            continue
        if not (0.0 < price < 1.0) or available_shares <= 0.0:
            continue
        fee_ps = pm_fee_per_share(price, rate, exponent)
        if fee_ps is None:
            return None
        cash_per_share = price + fee_ps
        take = min(available_shares, remaining_cost / cash_per_share)
        if take <= 0.0:
            continue
        gross_cost += take * price
        fee_total += take * fee_ps
        shares += take
        remaining_cost -= take * cash_per_share
        if remaining_cost <= max(1e-8, target_cost * 1e-10):
            break
    if shares <= 0.0 or remaining_cost > max(1e-8, target_cost * 1e-10):
        return None
    total_cost = gross_cost + fee_total
    return {
        "avg_price": gross_cost / shares,
        "shares": shares,
        "fee": fee_total,
        "total_cost": total_cost,
        "effective_multiplier": shares / total_cost,
    }


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
_STALE_THRESHOLD = RUNTIME_LIMIT_DEFAULTS["pm_book_stale_diagnostic_sec"]


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
    return bids, asks, _parse_clob_event_timestamp(book.get("timestamp"))


def pm_book_diagnostic(token_id, now=None):
    """Return cache state without treating an empty or stale book as executable."""
    key = str(token_id)
    now = time.time() if now is None else float(now)
    with LOCK:
        book = BOOKS.get(key) or BOOKS.get(token_id)
        if not book:
            state = "missing_book"
            age = None
            source_ts = None
            source_age = None
            bids, asks = [], []
        else:
            bids = list(book.get("bids") or [])
            asks = list(book.get("asks") or [])
            try:
                ts = float(book.get("ts") or 0.0)
            except (TypeError, ValueError):
                ts = 0.0
            age = max(0.0, now - ts) if ts > 0 else None
            source_ts = _book_source_timestamp(book)
            source_age = max(0.0, now - source_ts) if source_ts is not None else None
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
    attempted_at = refresh.get("attempted_at")
    started_at = refresh.get("started_at")
    try:
        queue_age = (max(0.0, now - float(attempted_at))
                     if refresh.get("state") == "queued" and attempted_at is not None
                     else max(0.0, float(started_at) - float(attempted_at))
                     if started_at is not None and attempted_at is not None
                     else None)
    except (TypeError, ValueError):
        queue_age = None
    try:
        fetch_age = (max(0.0, now - float(started_at))
                     if refresh.get("state") == "fetching" and started_at is not None
                     else None)
    except (TypeError, ValueError):
        fetch_age = None
    return {
        "book_state": state,
        "book_age_sec": round(age, 1) if age is not None else None,
        "source_event_ts": source_ts,
        "source_event_age_sec": round(source_age, 1) if source_age is not None else None,
        "bid_levels": len(bids),
        "ask_levels": len(asks),
        "best_bid": bids[0][0] if bids else None,
        "best_ask": asks[0][0] if asks else None,
        "rest_refresh": refresh.get("state", "not_requested"),
        "rest_attempted_at": attempted_at,
        "rest_started_at": started_at,
        "rest_completed_at": refresh.get("completed_at"),
        "rest_queue_age_sec": round(queue_age, 1) if queue_age is not None else None,
        "rest_fetch_age_sec": round(fetch_age, 1) if fetch_age is not None else None,
    }


def pm_book_health(token_ids, now=None):
    """Summarize local PM book receipt/source ages without triggering recovery."""
    now = time.time() if now is None else float(now)
    if isinstance(token_ids, (str, bytes)):
        token_ids = [token_ids]
    keys = {str(token).strip() for token in (token_ids or ())
            if token is not None and str(token).strip()}
    local_ages = []
    source_ages = []
    missing_books = 0
    missing_timestamps = 0
    fresh_books = 0
    stale_books = 0
    fresh_books_with_asks = 0

    with LOCK:
        for key in keys:
            book = BOOKS.get(key)
            if not isinstance(book, dict):
                missing_books += 1
                continue
            try:
                received_at = float(book.get("ts") or 0.0)
            except (TypeError, ValueError, OverflowError):
                received_at = 0.0
            if not math.isfinite(received_at) or received_at <= 0.0:
                missing_timestamps += 1
                continue
            age = max(0.0, now - received_at)
            local_ages.append(age)
            if age > BOOK_TTL:
                stale_books += 1
            else:
                fresh_books += 1
                if book.get("asks"):
                    fresh_books_with_asks += 1
            source_ts = _book_source_timestamp(book)
            if source_ts is not None:
                source_ages.append(max(0.0, now - source_ts))

    def summarize(values):
        values = sorted(value for value in values if math.isfinite(value) and value >= 0.0)
        if not values:
            return {"sample_count": 0, "p50_sec": None, "p95_sec": None, "max_sec": None}
        n = len(values)
        return {
            "sample_count": n,
            "p50_sec": round(values[max(0, math.ceil(0.50 * n) - 1)], 1),
            "p95_sec": round(values[max(0, math.ceil(0.95 * n) - 1)], 1),
            "max_sec": round(values[-1], 1),
        }

    return {
        "token_count": len(keys),
        "missing_books": missing_books,
        "missing_timestamps": missing_timestamps,
        "fresh_books": fresh_books,
        "stale_books": stale_books,
        "fresh_books_with_asks": fresh_books_with_asks,
        "book_ttl_sec": BOOK_TTL,
        "book_age_sec": summarize(local_ages),
        "source_event_age_sec": summarize(source_ages),
    }


def _refresh_pm_book_worker(token_id, attempted_at, reason, fresh_fetch=None, notify_update=None):
    key = str(token_id)
    result_state = "fetch_failed"
    bid_levels = ask_levels = 0
    applied = False
    started_at = time.time()
    with _PM_REFRESH_LOCK:
        previous = _PM_REFRESH_DIAGNOSTICS.get(key)
        if previous is not None:
            previous["state"] = "fetching"
            previous["started_at"] = started_at
    try:
        fresh = (fresh_fetch or _fresh_pm_book)(key, timeout=4.0)
        if fresh is not None:
            if not isinstance(fresh, (tuple, list)) or len(fresh) not in (2, 3):
                fresh = None
            elif len(fresh) == 2:
                bids, asks = fresh
                rest_source_ts = None
            else:
                bids, asks, rest_source_ts = fresh
                rest_source_ts = _parse_clob_event_timestamp(rest_source_ts)
        if fresh is not None:
            bid_levels, ask_levels = len(bids), len(asks)
            now = time.time()
            with LOCK:
                previous = BOOKS.get(key)
                try:
                    previous_ts = float((previous or {}).get("ts") or 0.0)
                except (TypeError, ValueError):
                    previous_ts = 0.0
                previous_source_ts = _book_source_timestamp(previous)
                comparable_source_times = (
                    rest_source_ts is not None and previous_source_ts is not None
                )
                if comparable_source_times and rest_source_ts < previous_source_ts:
                    result_state = "rest_snapshot_older"
                elif (previous_ts >= attempted_at
                      and (not comparable_source_times
                           or previous_source_ts >= rest_source_ts)):
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
                        "source_ts": (rest_source_ts if rest_source_ts is not None
                                      else previous_source_ts),
                    }
                    applied = True
                    if not bids and not asks:
                        result_state = "empty_book"
                    elif not asks:
                        result_state = "empty_asks"
                    else:
                        result_state = "asks_available"
            if applied:
                (notify_update or _notify_pm_update)(key)
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
                "started_at": previous.get("started_at", started_at),
                "completed_at": time.time(),
                "bid_levels": bid_levels,
                "ask_levels": ask_levels,
            }


def _schedule_pm_book_refresh(token_id, reason, executor=None, fresh_fetch=None, notify_update=None):
    key = str(token_id)
    now = time.time()
    with _PM_REFRESH_LOCK:
        last = _PM_REFRESH_COOLDOWN.get(key, 0.0)
        if key in _PM_REFRESH_INFLIGHT or now - last < _FRESH_MIN_INTERVAL:
            return False
        _PM_REFRESH_COOLDOWN[key] = now
        _PM_REFRESH_INFLIGHT.add(key)
        _PM_REFRESH_DIAGNOSTICS[key] = {
            "state": "queued", "reason": reason,
            "attempted_at": now, "completed_at": None,
            "started_at": None,
            "bid_levels": 0, "ask_levels": 0,
        }
    try:
        (executor or _PM_REFRESH_EXECUTOR).submit(
            _refresh_pm_book_worker, key, now, reason, fresh_fetch, notify_update)
    except Exception:
        with _PM_REFRESH_LOCK:
            _PM_REFRESH_INFLIGHT.discard(key)
            _PM_REFRESH_DIAGNOSTICS[key] = {
                "state": "schedule_failed", "reason": reason,
                "attempted_at": now, "completed_at": time.time(),
                "started_at": None,
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
