#!/usr/bin/env python3
import os, sys, subprocess, time, argparse
if sys.platform == 'win32': os.system('')
CAND = '2way_candidates.txt'
PY = sys.executable

def is_live(slug):
    s = slug.lower()
    for k in ['-game2', '-game3', '-game4', '-game5', '-set2', '-set3', '-set4', '-live', '-inplay']:
        if k in s: return True
    return False

def load_candidates():
    if not os.path.exists(CAND): return [], 0
    try:
        age = time.time() - os.path.getmtime(CAND)
        f = open(CAND, encoding='utf-8')
        lines = f.read().splitlines()
        f.close()
    except Exception:
        return [], 0
    if len(lines) < 2: return [], age
    out = []
    for ln in lines[1:]:
        parts = ln.split(chr(9))
        if len(parts) < 10: continue
        slug = parts[9]
        q = parts[10] if len(parts) > 10 else ''
        out.append({'sport': parts[0], 'tier': parts[1], 'over': parts[3], 'slug': slug, 'q': q, 'live': is_live(slug)})
    return out, age

def fmt_age(sec):
    if sec < 60: return str(int(sec)) + 's'
    return str(int(sec // 60)) + 'm'

def run_scan(sport, hours):
    cmd = [PY, 'scan_2way.py', '--only-low', '--hours', str(hours)]
    if sport: cmd.extend(['--sport', sport])
    print('[*] running: ' + ' '.join(cmd[1:]))
    subprocess.call(cmd)

def show_menu(items, age):
    print()
    print('=' * 100)
    print('  POLY-ARB launcher   age: ' + fmt_age(age))
    print('=' * 100)
    print()
    for i in range(min(20, len(items))):
        it = items[i]
        live = ' [LIVE]' if it['live'] else ''
        print('  ' + str(i + 1).rjust(2) + '  [' + it['sport'].ljust(8) + '] [' + it['tier'].ljust(3) + ']' + live + '  over=' + it['over'] + '  ' + it['q'][:55])
        print('      ' + it['slug'])
    print()

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--scan', action='store_true')
    ap.add_argument('--sport', default='')
    ap.add_argument('--hours', type=float, default=12.0)
    ap.add_argument('--force-live', action='store_true')
    args = ap.parse_args()
    if args.scan: run_scan(args.sport, args.hours)
    items, age = load_candidates()
    if not items:
        print('[!] no candidates in ' + CAND)
        return 1
    if age > 900:
        print('[!] file is ' + fmt_age(age) + ' old. Consider --scan')
    show_menu(items, age)
    print('Commands:')
    print('  1        launch match #1')
    print('  1 2 5    launch 1, 2, 5')
    print('  all      top-20')
    print('  c N      copy slug #N')
    print('  q        quit')
    print()
    cmd = input('> ').strip()
    if cmd.lower() == 'q': return 0
    if cmd.lower().startswith('c '):
        try: idx = int(cmd.split()[1]) - 1
        except Exception: return 1
        if idx < 0 or idx >= len(items): return 1
        if sys.platform == 'win32':
            p = subprocess.Popen(['clip'], stdin=subprocess.PIPE, shell=True)
            p.communicate(input=items[idx]['slug'].encode('utf-16-le'))
        print('[>] copied: ' + items[idx]['slug'])
        return 0
    if cmd.lower() == 'all':
        nums = list(range(1, min(20, len(items)) + 1))
    else:
        nums = []
        for p in cmd.replace(',', ' ').split():
            try: nums.append(int(p))
            except Exception: pass
    if not nums:
        print('[!] nothing selected')
        return 1
    selected = [items[n - 1] for n in nums if 1 <= n <= len(items)]
    if not selected:
        print('[!] no valid matches')
        return 1
    slugs = [it['slug'] for it in selected]
    use_live = args.force_live or any(it['live'] for it in selected)
    cmdline = PY + ' polyarb_v3.py --slugs ' + ','.join(slugs)
    if use_live: cmdline += ' --live'
    print()
    tag = ' [LIVE]' if use_live else ''
    print('[>] launching on ' + str(len(slugs)) + ' matches' + tag)
    for it in selected:
        t = ' [LIVE]' if it['live'] else ''
        print('    ' + it['slug'] + t)
    print()
    return subprocess.call(cmdline, shell=True)

sys.exit(main())
