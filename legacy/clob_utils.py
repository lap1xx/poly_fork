#!/usr/bin/env python3
# clob_utils.py - batch CLOB fetch + depth-aware liquidity + local live filter
import time
import requests

COLON = chr(58)
SLASH = chr(47)
DOT = chr(46)
HT = 'https' + COLON + SLASH + SLASH
CLOB = HT + 'clob' + DOT + 'polymarket' + DOT + 'com'
GAMMA = HT + 'gamma-api' + DOT + 'polymarket' + DOT + 'com'
TIMEOUT = 15.0

BOOK_CACHE = {}
CACHE_TTL = 5.0

def batch_books(token_ids, ttl=5.0):
    now = time.time()
    missing = []
    for t in token_ids:
        if t in BOOK_CACHE:
            ts, _ = BOOK_CACHE[t]
            if now - ts < ttl:
                continue
        missing.append(t)
    if not missing:
        return {t: BOOK_CACHE[t][1] for t in token_ids if t in BOOK_CACHE}
    payload = [{'token_id': t} for t in missing]
    url = CLOB + '/books'
    try:
        r = requests.post(url, json=payload, timeout=TIMEOUT)
        r.raise_for_status()
        books = r.json()
    except Exception:
        return {t: BOOK_CACHE[t][1] for t in token_ids if t in BOOK_CACHE}
    for b in books:
        aid = b.get('asset_id', '')
        if not aid:
            continue
        bids = []
        asks = []
        for x in b.get('bids', []) or []:
            try:
                sz = float(x.get('size', 0))
                if sz > 0:
                    bids.append((float(x.get('price', 0)), sz))
            except Exception:
                pass
        for x in b.get('asks', []) or []:
            try:
                sz = float(x.get('size', 0))
                if sz > 0:
                    asks.append((float(x.get('price', 0)), sz))
            except Exception:
                pass
        bids.sort(key=lambda z: -z[0])
        asks.sort(key=lambda z: z[0])
        BOOK_CACHE[aid] = (time.time(), {'bids': bids, 'asks': asks})
    return {t: BOOK_CACHE[t][1] for t in token_ids if t in BOOK_CACHE}

def walk_avg(levels, target_size):
    rem = target_size
    total = 0.0
    for p, s in levels:
        t = min(rem, s)
        total += t * p
        rem -= t
        if rem <= 1e-9:
            break
    if rem > 1e-9:
        return None
    return total / target_size

def depth_liquidity(book, target_usd=200.0):
    if not book:
        return 0.0, 0.0, 0.0, 0.0
    asks = book.get('asks', []) or []
    bids = book.get('bids', []) or []
    if not asks:
        return 0.0, 0.0, 0.0, 0.0
    best_ask = asks[0][0]
    best_ask_size = asks[0][1]
    total_notional = 0.0
    total_size = 0.0
    for p, s in asks:
        total_notional += p * s
        total_size += s
    best_bid = bids[0][0] if bids else 0.0
    spread = best_ask - best_bid if best_bid > 0 else 0.0
    return best_ask, best_ask_size, total_notional, spread

def local_is_live(ev):
    try:
        if ev.get('closed') is True:
            return False
        if ev.get('active') is False:
            return False
        st = ev.get('startTime') or ev.get('eventStartTime') or ''
        if not st:
            return False
        import datetime
        s = st.replace('Z', '+00:00').replace(' ', 'T')
        dt = datetime.datetime.fromisoformat(s)
        now = datetime.datetime.now(datetime.timezone.utc)
        return dt <= now
    except Exception:
        return False

def health_check():
    try:
        r = requests.get(GAMMA + '/markets', params={'limit': 1}, timeout=10)
        return r.status_code == 200
    except Exception:
        return False
