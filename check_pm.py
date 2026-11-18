#!/usr/bin/env python3
"""
check_pm.py - Polymarket depth checker.

Показывает реальную глубину PM-стакана для одного или нескольких слагов:
  best ask/bid, spread, средняя цена для ставок $20/$50/$100/$200,
  кэф с учётом 4% PM-комиссии.

USAGE
    python check_pm.py cs2-mw-yaw-2026-09-26-game1
    python check_pm.py --last 3                 # последние 3 вилки из arbs.jsonl
    python check_pm.py slug1 slug2 slug3
"""

import argparse
import json
import os
import sys
import urllib.parse
import urllib.request

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
PM_FEE = 0.04
BET_SIZES_USD = [20, 50, 100, 200, 500]

def _get(url, params=None, timeout=15):
    if params:
        url = url + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": "poly-arb/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))

def fetch_market(slug):
    try:
        d = _get(f"{GAMMA}/markets", {"slug": slug})
    except Exception as e:
        return None, f"gamma error: {e}"
    if not d:
        return None, "not found on gamma"
    m = d[0]
    try:
        outcomes = json.loads(m["outcomes"])
        tokens = json.loads(m["clobTokenIds"])
    except Exception as e:
        return None, f"parse error: {e}"
    return {
        "slug": m.get("slug"),
        "q": m.get("question", ""),
        "outcomes": outcomes,
        "tokens": tokens,
        "closed": m.get("closed", False),
        "accepting": m.get("acceptingOrders", True),
        "best_bid": m.get("bestBid"),
        "best_ask": m.get("bestAsk"),
        "last": m.get("lastTradePrice"),
        "volume": m.get("volume"),
        "liquidity": m.get("liquidity"),
    }, None

def fetch_book(token_id):
    try:
        d = _get(f"{CLOB}/book", {"token_id": token_id})
    except Exception as e:
        return None, f"clob error: {e}"
    bids = []
    asks = []
    for x in d.get("bids") or []:
        try:
            p = float(x.get("price", 0))
            s = float(x.get("size", 0))
            if p > 0 and s > 0:
                bids.append((p, s))
        except Exception:
            pass
    for x in d.get("asks") or []:
        try:
            p = float(x.get("price", 0))
            s = float(x.get("size", 0))
            if p > 0 and s > 0:
                asks.append((p, s))
        except Exception:
            pass
    bids.sort(key=lambda z: -z[0])
    asks.sort(key=lambda z: z[0])
    return {"bids": bids, "asks": asks}, None

def walk(levels, target_usd):
    """Returns (avg_price, filled_usd, fully_filled) or (None, 0, False)."""
    rem = target_usd
    total = 0.0
    spent = 0.0
    for p, s in levels:
        lvl_usd = p * s
        take = min(rem, lvl_usd)
        if take <= 0:
            continue
        total += take / p
        spent += take
        rem -= take
        if rem <= 1e-6:
            break
    if total <= 0:
        return None, 0.0, False
    avg = spent / total
    return avg, spent, rem <= 1e-6

def fmt_odds(p):
    if not p or p <= 0:
        return "-"
    return f"{(1.0 / p):.4f}"

def fmt_odds_fee(p):
    if not p or p <= 0:
        return "-"
    return f"{(1.0 / p) / (1.0 + PM_FEE):.4f}"

def check_slug(slug):
    print()
    print("=" * 78)
    print(f"  PM CHECK: {slug}")
    print("=" * 78)
    m, err = fetch_market(slug)
    if err:
        print(f"  [!] {err}")
        return
    print(f"  {m['q'][:74]}")
    print(f"  closed={m['closed']}  accepting={m['accepting']}  "
          f"volume={m.get('volume')}  liquidity={m.get('liquidity')}")
    print()

    for i, oc in enumerate(m["outcomes"]):
        tid = m["tokens"][i] if i < len(m["tokens"]) else None
        print(f"  --- [{i}] {oc} ---")
        if not tid:
            print("      no token")
            continue
        book, err = fetch_book(tid)
        if err:
            print(f"      [!] {err}")
            continue
        bids, asks = book["bids"], book["asks"]
        if not bids and not asks:
            print("      EMPTY BOOK")
            continue
        bb = bids[0] if bids else None
        ba = asks[0] if asks else None
        print(f"      best bid: {bb[0]:.4f} x {bb[1]:.0f}" if bb else "      best bid: -")
        print(f"      best ask: {ba[0]:.4f} x {ba[1]:.0f}" if ba else "      best ask: -")
        if bb and ba:
            print(f"      spread:   {ba[0] - bb[0]:.4f}")
        print(f"      ask depth: {len(asks)} levels, "
              f"total ${sum(p * s for p, s in asks):.0f}")
        print(f"      bid depth: {len(bids)} levels, "
              f"total ${sum(p * s for p, s in bids):.0f}")
        print()
        print(f"      {'size $':>8}  {'avg price':>10}  {'odds':>9}  "
              f"{'odds -fee':>10}  {'filled':>8}")
        for usd in BET_SIZES_USD:
            avg, spent, full = walk(asks, usd)
            if avg is None:
                print(f"      {usd:>8}  {'-- no asks --':>10}")
                continue
            flag = "" if full else "  !partial"
            print(f"      {usd:>8}  {avg:>10.4f}  {fmt_odds(avg):>9}  "
                  f"{fmt_odds_fee(avg):>10}  {spent:>7.0f}{flag}")
        print()

def last_slugs_from_jsonl(path, n=3):
    if not os.path.exists(path):
        return []
    slugs = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except Exception:
        return []
    for line in reversed(lines):
        try:
            rec = json.loads(line)
            s = rec.get("slug")
            if s and s not in slugs:
                slugs.append(s)
            if len(slugs) >= n:
                break
        except Exception:
            continue
    return slugs

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("slugs", nargs="*", help="PM slugs")
    ap.add_argument("--last", type=int, default=0,
                    help="check last N slugs from arbs.jsonl")
    ap.add_argument("--jsonl", default="arbs.jsonl")
    args = ap.parse_args()

    slugs = list(args.slugs)
    if args.last:
        more = last_slugs_from_jsonl(args.jsonl, args.last)
        for s in more:
            if s not in slugs:
                slugs.append(s)

    if not slugs:
        print("[!] no slugs. Use: python check_pm.py <slug>  |  --last 3")
        return 1

    for s in slugs:
        try:
            check_slug(s)
        except KeyboardInterrupt:
            print("\n[*] interrupted")
            return 0
        except Exception as e:
            print(f"  [!] {s}: {type(e).__name__}: {e}")
    return 0

if __name__ == "__main__":
    sys.exit(main())