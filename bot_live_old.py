#!/usr/bin/env python3
# bot_live.py v5 - AUTO mode default, no input prompt.
import argparse, concurrent.futures, json, os, subprocess, sys, time, requests
from clob_utils import batch_books, walk_avg, depth_liquidity, local_is_live
if sys.platform == 'win32': os.system('')

PY = sys.executable
POLYARB = 'polyarb_v4.py'
COLON = chr(58); SLASH = chr(47); DOT = chr(46)
HT = 'https' + COLON + SLASH + SLASH
GAMMA = HT + 'gamma-api' + DOT + 'polymarket' + DOT + 'com'
TIMEOUT = 15.0

SPORTS = [('cs2-','CS2'),('dota2-','DOTA2'),('lol-','LOL'),('valorant-','VALORANT'),
          ('atp-','TENNIS'),('wta-','TENNIS'),('itf-','TENNIS')]
LOW_LEAGUES = ['logitech','play-connect','g-play','elisa','showmatch','show-cup','cct','esea-open','esea-intermediate','esea-advanced','esea-md','united21','nodwin','challenger','qualifier','european-pro-league','cis-lan','academy','youth','junior','reserves','w15','w25','w40','w60','w75','w100','itf','m15','m25','winline-insight','beast-cup','fantasy-cup','gamers-club','esports-battle','cups','boom-cup','rising-stars','talent','development','amateur','global-esports','cyber-battle','tiktok','youtube','twitch','kick','streamers','influencer','media','content','fame','celebrity']
STREAMER_KEYS = ['streamer','showmatch','show-match','influencer','charity','pro-am','proam','star-series','esforce','betboom','winline','parimatch','1win','boom-cup','cyber-battle','esports-battle','streamers-battle','media-tournament','blitz-cup','show-cup','beast-cup','kick','logitech','play-connect']

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
    b = batch_books([tid], ttl=10.0).get(tid)
    if not b: return None
    asks = b.get('asks') or []
    if not asks: return (0.0, 0.0)
    return (asks[0][0], asks[0][1])

def market_liq(m):
    try:
        toks = json.loads(m.get('clobTokenIds', '[]'))
    except Exception:
        return 0.0, 0.0
    if len(toks) != 2: return 0.0, 0.0
    a0 = best_ask(toks[0]); a1 = best_ask(toks[1])
    if a0 is None or a1 is None: return 0.0, 0.0
    p0, s0 = a0; p1, s1 = a1
    return p0 + p1, min(p0 * s0, p1 * s1)

def fetch_live():
    return req(GAMMA + '/events', params={'tag_id': 100639, 'live': 'true',
        'active': 'true', 'closed': 'false', 'limit': 200}) or []

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
    all_tokens = []
    for ev, slug, title, sport, low in pass_list:
        for m in (ev.get('markets', []) or []):
            if m.get('closed') is True: continue
            if m.get('acceptingOrders') is False: continue
            try:
                all_tokens.extend(json.loads(m.get('clobTokenIds', '[]')))
            except Exception:
                pass
    print('[*] prefetching ' + str(len(all_tokens)) + ' token books...')
    batch_books(all_tokens, ttl=15.0)
    all_markets = []
    for ev, slug, title, sport, low in pass_list:
        for m in (ev.get('markets', []) or []):
            if m.get('closed') is True: continue
            if m.get('acceptingOrders') is False: continue
            all_markets.append(m)
    print('[*] ' + str(len(all_markets)) + ' markets (x' + str(workers) + ')')
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
    for ev, slug, title, sport, low in pass_list:
        good = []
        for m in (ev.get('markets', []) or []):
            ms = m.get('slug', '')
            if ms in rmap: good.append(rmap[ms])
        if not good: continue
        good.sort(key=lambda x: x['over'])
        start_time = ev.get('startTime', '') or ev.get('eventStartTime', '') or ''
        out.append({'event_slug': slug, 'sport': sport, 'title': title,
            'start': start_time, 'low': low,
            'streamer': is_streamer(slug, title), 'markets': good})
    return out

def show_menu(items, fetched_at):
    print()
    print('=' * 105)
    print('  POLY-ARB LIVE launcher   events: ' + str(len(items)) + '   ' + fetched_at)
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
        if it['start']: print('      start: ' + it['start'])
        for j, m in enumerate(it['markets']):
            tag = chr(97 + j)
            print('        ' + tag + ') over=' + format(m['over'], '.3f')
                  + '  liq=' + format(m['liq'], ',.0f') + '  ' + m['question'][:70])
            print('           ' + m['slug'])
        n += 1
    print()

def run_auto(args):
    cmd = (PY + ' ' + POLYARB + ' --watch --live'
           + ' --poll 0.3 --alert-gap 30 --min-roi ' + str(args.min_roi))
    if args.no_bb: cmd += ' --no-bb'
    print()
    print('[>] AUTO mode:')
    print('    ' + cmd)
    print()
    try:
        return subprocess.call(cmd, shell=True)
    except KeyboardInterrupt:
        print()
        print('stopped')
        return 0

def run_menu(args):
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
            print('[!] no live events.')
            if args.refresh > 0:
                time.sleep(args.refresh); continue
            return 1
        print('Commands: 1a (market a of event 1) | 1 (all of event 1) | all | r | q')
        try:
            cmd = input('> ').strip()
        except EOFError:
            return 0
        if cmd.lower() == 'q': return 0
        if cmd.lower() == 'r':
            last_fetch = 0; continue
        slugs = []
        for tok in cmd.replace(',', ' ').split():
            if tok.lower() == 'all':
                for it in items[:10]:
                    for m in it['markets']: slugs.append(m['slug'])
                continue
            t = tok.lower(); ev_idx = -1; m_idx = -1
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
            if args.refresh > 0: continue
            return 1
        cmdline = PY + ' ' + POLYARB + ' --slugs ' + ','.join(slugs) + ' --live'
        print()
        print('[>] launching LIVE on ' + str(len(slugs)) + ' markets:')
        for s in slugs: print('    ' + s)
        print()
        try:
            rc = subprocess.call(cmdline, shell=True)
        except KeyboardInterrupt:
            print(); print('stopped'); rc = 0
        last_fetch = 0
        if args.refresh == 0: return rc

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--auto', action='store_true', help='auto mode (default)')
    ap.add_argument('--menu', action='store_true', help='interactive menu')
    ap.add_argument('--sport', default='')
    ap.add_argument('--only-low', action='store_true')
    ap.add_argument('--min-liq', type=float, default=20)
    ap.add_argument('--refresh', type=int, default=0)
    ap.add_argument('--workers', type=int, default=20)
    ap.add_argument('--min-roi', type=float, default=7.0)
    ap.add_argument('--no-bb', action='store_true')
    args = ap.parse_args()
    if args.menu and not args.auto:
        return run_menu(args)
    return run_auto(args)

try:
    sys.exit(main() or 0)
except KeyboardInterrupt:
    print()
    print('interrupted')
