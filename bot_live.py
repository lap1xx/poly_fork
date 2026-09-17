#!/usr/bin/env python3
# bot_live.py v4 - no stale score, live-refresh
import argparse, concurrent.futures, json, os, subprocess, sys, time, requests
if sys.platform == 'win32': os.system('')

PY = sys.executable
COLON = chr(58)
SLASH = chr(47)
DOT = chr(46)
HT = 'https' + COLON + SLASH + SLASH
GAMMA = HT + 'gamma-api' + DOT + 'polymarket' + DOT + 'com'
CLOB = HT + 'clob' + DOT + 'polymarket' + DOT + 'com'
TIMEOUT = 15.0

SPORTS = [
    ('cs2-', 'CS2'),
    ('dota2-', 'DOTA2'),
    ('lol-', 'LOL'),
    ('valorant-', 'VALORANT'),
    ('atp-', 'TENNIS'),
    ('wta-', 'TENNIS'),
    ('itf-', 'TENNIS'),
]

LOW_LEAGUES = ['logitech', 'play-connect', 'g-play', 'elisa', 'showmatch', 'show-cup', 'cct', 'esea-open', 'esea-intermediate', 'esea-advanced', 'esea-md', 'united21', 'nodwin', 'challenger', 'qualifier', 'european-pro-league', 'cis-lan', 'academy', 'youth', 'junior', 'reserves', 'w15', 'w25', 'w40', 'w60', 'w75', 'w100', 'itf', 'm15', 'm25', 'winline-insight', 'beast-cup', 'fantasy-cup', 'gamers-club', 'esports-battle', 'cups', 'boom-cup', 'rising-stars', 'talent', 'development', 'amateur', 'global-esports', 'cyber-battle', 'tiktok', 'youtube', 'twitch', 'kick', 'streamers', 'influencer', 'media', 'content', 'fame', 'celebrity']
STREAMER_KEYS = ['streamer', 'showmatch', 'show-match', 'influencer', 'charity', 'pro-am', 'proam', 'star-series', 'esforce', 'betboom', 'winline', 'parimatch', '1win', 'boom-cup', 'cyber-battle', 'esports-battle', 'streamers-battle', 'media-tournament', 'blitz-cup', 'show-cup', 'beast-cup', 'kick', 'logitech', 'play-connect']

def sport_of(slug):
    s = slug.lower()
    for p, n in SPORTS:
        if s.startswith(p): return n
    return ''

def is_low(slug, q):
    s = (slug + ' ' + q).lower()
    for k in LOW_LEAGUES:
        if k in s: return True
    return False

def is_streamer(slug, q):
    s = (slug + ' ' + q).lower()
    for k in STREAMER_KEYS:
        if k in s: return True
    return False

def req(url, params=None):
    try:
        r = requests.get(url, params=params, timeout=TIMEOUT)
        if r.status_code == 404: return None
        r.raise_for_status()
        return r.json()
    except Exception:
        return None

def best_ask(tid):
    d = req(CLOB + '/book', params={'token_id': tid})
    if not d: return None
    asks = d.get('asks') or []
    if not asks: return (0.0, 0.0)
    b = min(asks, key=lambda x: float(x['price']))
    return (float(b['price']), float(b['size']))

def market_liq(m):
    try:
        tokens = json.loads(m.get('clobTokenIds', '[]'))
    except Exception:
        return 0.0, 0.0
    if len(tokens) != 2: return 0.0, 0.0
    a0 = best_ask(tokens[0])
    a1 = best_ask(tokens[1])
    if a0 is None or a1 is None: return 0.0, 0.0
    p0, s0 = a0
    p1, s1 = a1
    return p0 + p1, min(p0 * s0, p1 * s1)

def fetch_live():
    return req(GAMMA + '/events', params={'active': 'true', 'closed': 'false', 'limit': 200, 'live': 'true'}) or []

def market_task(m):
    mslug = m.get('slug', '')
    mq = m.get('question', '')
    if not mslug: return None
    over, liq = market_liq(m)
    return {'slug': mslug, 'question': mq, 'over': over, 'liq': liq}


def collect_events(sport_filter='', only_low=False, min_liq=20, workers=20):
    events = fetch_live()
    pass_list = []
    for ev in events:
        slug = ev.get('slug', '')
        title = ev.get('title', '')
        sport = sport_of(slug)
        if sport == '': continue
        if sport_filter and sport.upper() != sport_filter.upper(): continue
        low = is_low(slug, title)
        if only_low and not low: continue
        pass_list.append((ev, slug, title, sport, low))
    print('[*] ' + str(len(pass_list)) + ' events pass pre-filter')
    all_markets = []
    for tup in pass_list:
        ev, slug, title, sport, low = tup
        markets = ev.get('markets', []) or []
        for m in markets:
            if m.get('closed') is True: continue
            if m.get('acceptingOrders') is False: continue
            all_markets.append(m)
    print('[*] ' + str(len(all_markets)) + ' markets (parallel x' + str(workers) + ')')
    rmap = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {ex.submit(market_task, m): m for m in all_markets}
        done = 0
        for fut in concurrent.futures.as_completed(futures):
            done += 1
            if done % 20 == 0:
                print('    ' + str(done) + ' / ' + str(len(futures)))
            try:
                r = fut.result()
            except Exception:
                r = None
            if r is None: continue
            if r['liq'] < min_liq: continue
            rmap[r['slug']] = r
    out = []
    for tup in pass_list:
        ev, slug, title, sport, low = tup
        markets = ev.get('markets', []) or []
        good = []
        for m in markets:
            ms = m.get('slug', '')
            if ms in rmap:
                good.append(rmap[ms])
        if not good: continue
        good.sort(key=lambda x: x['over'])
        start_time = ev.get('startTime', '') or ev.get('eventStartTime', '') or ''
        out.append({'event_slug': slug, 'sport': sport, 'title': title, 'start': start_time, 'low': low, 'streamer': is_streamer(slug, title), 'markets': good})
    return out
def show_menu(items, fetched_at):
    print()
    print('=' * 105)
    print('  POLY-ARB LIVE launcher   events: ' + str(len(items)) + '   fetched: ' + fetched_at)
    print('=' * 105)
    if not items:
        print('  (no live events with liquid markets)')
        return
    n = 1
    for it in items:
        flags = ''
        if it['low']: flags += ' [LOW]'
        if it['streamer']: flags += ' [STREAMER]'
        print()
        print('  [' + str(n) + '] [' + it['sport'].ljust(8) + ']' + flags + '  ' + it['title'][:80])
        if it['start']:
            print('      start: ' + it['start'])
        for j, m in enumerate(it['markets']):
            tag = chr(97 + j)
            print('        ' + tag + ') over=' + format(m['over'], '.3f') + '  liq=' + format(m['liq'], ',.0f') + '  ' + m['question'][:70])
            print('           ' + m['slug'])
        n += 1
    print()

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--sport', default='')
    ap.add_argument('--only-low', action='store_true')
    ap.add_argument('--min-liq', type=float, default=20)
    ap.add_argument('--refresh', type=int, default=0)
    ap.add_argument('--workers', type=int, default=20)
    args = ap.parse_args()
    last_fetch = 0
    items = []
    while True:
        if time.time() - last_fetch >= args.refresh or last_fetch == 0:
            print('[*] fetching live events...')
            items = collect_events(args.sport, args.only_low, args.min_liq, args.workers)
            last_fetch = time.time()
            fetched_at = time.strftime('%H:%M:%S')
        else:
            fetched_at = time.strftime('%H:%M:%S', time.localtime(last_fetch))
        show_menu(items, fetched_at)
        if not items:
            print('[!] no live events. try again later or lower --min-liq')
            if args.refresh > 0:
                time.sleep(args.refresh)
                continue
            return 1
        print('Commands:')
        print('  1a       launch market a of event 1')
        print('  1a 1b    launch two markets')
        print('  1        launch all liquid markets of event 1')
        print('  all      all markets of top-10')
        print('  r        refresh')
        print('  q        quit')
        print()
        try:
            cmd = input('> ').strip()
        except EOFError:
            return 0
        if cmd.lower() == 'q': return 0
        if cmd.lower() == 'r':
            last_fetch = 0
            continue
        slugs = []
        tokens = cmd.replace(',', ' ').split()
        for tok in tokens:
            if tok.lower() == 'all':
                for it in items[:10]:
                    for m in it['markets']: slugs.append(m['slug'])
                continue
            t = tok.lower()
            ev_idx = -1
            m_idx = -1
            if len(t) >= 1 and t[0].isdigit():
                j = 0
                while j < len(t) and t[j].isdigit(): j += 1
                try: ev_idx = int(t[:j]) - 1
                except Exception: ev_idx = -1
                if j < len(t) and t[j].isalpha(): m_idx = ord(t[j]) - ord('a')
            if ev_idx < 0 or ev_idx >= len(items): continue
            it = items[ev_idx]
            if m_idx < 0:
                for m in it['markets']: slugs.append(m['slug'])
            elif m_idx < len(it['markets']):
                slugs.append(it['markets'][m_idx]['slug'])
        if not slugs:
            print('[!] nothing selected')
            if args.refresh > 0:
                continue
            return 1
        cmdline = PY + ' polyarb_v3.py --slugs ' + ','.join(slugs) + ' --live'
        print()
        print('[>] launching LIVE on ' + str(len(slugs)) + ' markets:')
        for s in slugs: print('    ' + s)
        print()
        try:
            rc = subprocess.call(cmdline, shell=True)
        except KeyboardInterrupt:
            print()
            print('stopped')
            rc = 0
        last_fetch = 0
        if args.refresh == 0:
            return rc


try:
    main()
except KeyboardInterrupt:
    print()
    print('interrupted')
