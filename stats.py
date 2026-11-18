#!/usr/bin/env python3
# stats.py - stats for Poly-ARB (reads arbs.jsonl, prints dashboard).
# v1: 22.09.2026
#
# Usage:
#   python stats.py                       # full report
#   python stats.py --csv stats.csv       # + export best-per-slug to CSV
#   python stats.py --since 2026-09-22    # only since date (YYYY-MM-DD)
#   python stats.py --min-roi 5           # only arbs with roi >= 5%
#   python stats.py --file arbs.jsonl     # custom input file
#
# Reads arbs.jsonl (one JSON per line), produced by polyarb_v4.py log_arb_json().
from __future__ import annotations
import argparse
import collections as C
import csv
import json
import os
import statistics as S
import sys
from datetime import datetime

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

def sep(ch="=", n=74):
    return ch * n

def load(path):
    if not os.path.exists(path):
        print(f"[!] file not found: {path}")
        sys.exit(1)
    out = []
    bad = 0
    with open(path, encoding="utf-8") as f:
        for ln in f:
            ln = ln.strip()
            if not ln:
                continue
            try:
                out.append(json.loads(ln))
            except Exception:
                bad += 1
    return out, bad

def filter_rows(rows, since=None, min_roi=None, sport=None, mtype=None):
    out = []
    since_dt = None
    if since:
        try:
            since_dt = datetime.fromisoformat(since)
        except Exception:
            print(f"[!] bad --since: {since}")
            sys.exit(1)
    for d in rows:
        if since_dt is not None:
            try:
                ts = datetime.fromisoformat(d["ts"])
                if ts < since_dt:
                    continue
            except Exception:
                continue
        if min_roi is not None and d.get("roi", 0) * 100 < min_roi:
            continue
        if sport and d.get("sport") != sport:
            continue
        if mtype and d.get("market_type") != mtype:
            continue
        out.append(d)
    return out

def print_header(rows, bad):
    n = len(rows)
    if n == 0:
        print("[!] no rows after filter")
        return
    slugs = {d["slug"] for d in rows}
    sports = C.Counter(d.get("sport", "?") for d in rows)
    types = C.Counter(d.get("market_type", "?") for d in rows)
    live = sum(1 for d in rows if d.get("live"))
    streamer = sum(1 for d in rows if d.get("streamer"))
    ts0 = min(d["ts"] for d in rows)
    ts1 = max(d["ts"] for d in rows)

    print(sep("="))
    print("  POLY-ARB  ---  arbs.jsonl statistics")
    print(sep("="))
    print(f"  Rows total:        {n}" + (f"  (bad: {bad})" if bad else ""))
    print(f"  Unique slugs:      {len(slugs)}")
    print(f"  Updates per slug:  {n / max(1, len(slugs)):.1f}")
    print(f"  Time range:        {ts0}  ->  {ts1}")
    print(f"  Live:              {live}  ({live*100//max(1,n)}%)")
    print(f"  Streamer:          {streamer}  ({streamer*100//max(1,n)}%)")
    print()
    print("  By sport:  " + "  ".join(f"{k}={v}" for k, v in sports.most_common()))
    print("  By type:   " + "  ".join(f"{k}={v}" for k, v in types.most_common()))
    print()

def print_roi_block(rows, label="ALL"):
    if not rows:
        return
    R = [d["roi"] * 100 for d in rows]
    P = [d.get("profit_rub", 0) for d in rows]
    print(sep("-"))
    print(f"  ROI ({label}, n={len(R)})")
    print(sep("-"))
    print(f"    median  {S.median(R):7.2f}%")
    print(f"    mean    {S.mean(R):7.2f}%")
    print(f"    min     {min(R):7.2f}%")
    print(f"    max     {max(R):7.2f}%")
    print(f"    >=5%:   {sum(1 for r in R if r >= 5):4d}   "
          f">=10%: {sum(1 for r in R if r >= 10):4d}   "
          f">=20%: {sum(1 for r in R if r >= 20):4d}   "
          f">=35%: {sum(1 for r in R if r >= 35):4d}")
    print()
    print(f"  Profit per row (RUB, n={len(P)})")
    print(f"    median  {S.median(P):8.0f}")
    print(f"    mean    {S.mean(P):8.0f}")
    print(f"    max     {max(P):8.0f}")
    print()

def print_grouped(rows, key, title):
    g = C.defaultdict(list)
    for d in rows:
        g[d.get(key, "?")].append(d["roi"] * 100)
    print(sep("-"))
    print(f"  Median ROI by {title}")
    print(sep("-"))
    print(f"    {'key':<12}{'n':>6}{'median':>10}{'mean':>10}{'max':>10}")
    for k, v in sorted(g.items(), key=lambda x: -len(x[1])):
        print(f"    {k:<12}{len(v):>6}{S.median(v):>9.2f}%{S.mean(v):>9.2f}%{max(v):>9.2f}%")
    print()

def print_top(rows, n=10):
    print(sep("-"))
    print(f"  Top-{n} by ROI")
    print(sep("-"))
    best = sorted(rows, key=lambda d: -d["roi"])[:n]
    for d in best:
        print("    %+6.1f%%  %-8s %-8s  %-42s  profit %5.0fR"
              % (d["roi"] * 100, d.get("sport", "?"),
                 d.get("market_type", "?"), d["slug"][:42],
                 d.get("profit_rub", 0)))
    print()

def print_persistence(rows, n=15):
    c = C.Counter(d["slug"] for d in rows)
    print(sep("-"))
    print(f"  Top-{n} most persistent (updates per slug)")
    print(sep("-"))
    for slug, cnt in c.most_common(n):
        # best ROI for this slug
        best = max((d["roi"] * 100 for d in rows if d["slug"] == slug), default=0)
        print(f"    {cnt:>4}x  {slug[:50]:<50}  best {best:+6.1f}%")
    print()

def dedup_best_per_slug(rows):
    best = {}
    for d in rows:
        s = d["slug"]
        if s not in best or d["roi"] > best[s]["roi"]:
            best[s] = d
    return list(best.values())

def print_honest_estimate(rows):
    best = dedup_best_per_slug(rows)
    if not best:
        return
    R = [d["roi"] * 100 for d in best]
    P = [d.get("profit_rub", 0) for d in best]
    print(sep("-"))
    print(f"  Honest estimate (best per slug, n={len(best)})")
    print(sep("-"))
    print(f"    ROI median       {S.median(R):.2f}%")
    print(f"    ROI mean         {S.mean(R):.2f}%")
    print(f"    Profit per trade median  {S.median(P):.0f}R")
    print(f"    Total if hit all:        {sum(P):.0f}R")
    days = max(1, len({d["ts"][:10] for d in rows}))
    print(f"    Days in file:            {days}")
    print(f"    ~ per day:               {sum(P)/days:.0f}R  "
          f"(~${sum(P)/days/85:.0f})")
    print()
    print("    NOTE: this is upper bound. Real fill rate ~30-50%.")
    print()

def export_csv(rows, path):
    best = dedup_best_per_slug(rows)
    best.sort(key=lambda d: -d["roi"])
    cols = ["ts", "slug", "sport", "market_type", "live", "streamer",
            "roi", "bank_rub", "profit_rub", "payout_rub", "rate_usdrub"]
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(cols)
        for d in best:
            w.writerow([d.get(c, "") for c in cols])
    print(f"[*] wrote {len(best)} rows -> {path}")

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", default="arbs.jsonl")
    ap.add_argument("--csv", default="")
    ap.add_argument("--since", default="")
    ap.add_argument("--min-roi", type=float, default=None)
    ap.add_argument("--sport", default="")
    ap.add_argument("--type", default="")
    args = ap.parse_args()

    rows, bad = load(args.file)
    rows = filter_rows(rows, since=args.since or None,
                       min_roi=args.min_roi,
                       sport=args.sport or None,
                       mtype=args.type or None)

    print_header(rows, bad)
    if not rows:
        return
    print_roi_block(rows, label="ALL")
    print_grouped(rows, "sport", "sport")
    print_grouped(rows, "market_type", "market type")
    print_top(rows, n=10)
    print_persistence(rows, n=15)
    print_honest_estimate(rows)

    if args.csv:
        export_csv(rows, args.csv)


if __name__ == "__main__":
    main()
