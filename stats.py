#!/usr/bin/env python3
"""Statistics for Poly-ARB signal snapshots (not executed trades)."""
from __future__ import annotations

import argparse
import collections as C
import csv
import json
import math
import os
import statistics as S
import sys
from datetime import datetime, timezone

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass


def sep(ch="=", n=74):
    return ch * n


def _parse_timestamp(value):
    """Parse ISO timestamps consistently as UTC; naive legacy values are local."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError("timestamp is empty")
    dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.astimezone()
    return dt.astimezone(timezone.utc)


def _finite_number(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def load(path):
    if not os.path.exists(path):
        print(f"[!] file not found: {path}")
        sys.exit(1)
    out = []
    bad = 0
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
                if not isinstance(row, dict):
                    raise ValueError("row is not an object")
                if not isinstance(row.get("slug"), str) or not row["slug"].strip():
                    raise ValueError("missing slug")
                if _finite_number(row.get("roi")) is None:
                    raise ValueError("invalid roi")
                row["roi"] = float(row["roi"])
                profit = _finite_number(row.get("profit_rub", 0))
                row["profit_rub"] = profit if profit is not None else 0.0
                out.append(row)
            except Exception:
                bad += 1
    return out, bad


def filter_rows(rows, since=None, min_roi=None, sport=None, mtype=None):
    out = []
    since_dt = None
    if since:
        try:
            since_dt = _parse_timestamp(since)
        except (TypeError, ValueError):
            print(f"[!] bad --since: {since}")
            sys.exit(1)
    for row in rows:
        if not isinstance(row, dict):
            continue
        if since_dt is not None:
            try:
                row_dt = _parse_timestamp(row.get("ts"))
            except (TypeError, ValueError):
                continue
            if row_dt < since_dt:
                continue
        roi = _finite_number(row.get("roi"))
        if roi is None:
            continue
        if min_roi is not None and roi * 100 < min_roi:
            continue
        if sport and row.get("sport") != sport:
            continue
        if mtype and row.get("market_type") != mtype:
            continue
        out.append(row)
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
    ts_values = [str(d.get("ts", "?")) for d in rows]

    print(sep("="))
    print("  POLY-ARB  ---  arbs.jsonl signal statistics")
    print(sep("="))
    print(f"  Valid signal rows: {n}" + (f"  (bad: {bad})" if bad else ""))
    print(f"  Unique slugs:      {len(slugs)}")
    print(f"  Updates per slug:  {n / max(1, len(slugs)):.1f}")
    print(f"  Time range:        {min(ts_values)}  ->  {max(ts_values)}")
    print(f"  Live:              {live}  ({live*100//max(1,n)}%)")
    print(f"  Streamer:          {streamer}  ({streamer*100//max(1,n)}%)")
    print()
    print("  By sport:  " + "  ".join(f"{k}={v}" for k, v in sports.most_common()))
    print("  By type:   " + "  ".join(f"{k}={v}" for k, v in types.most_common()))
    print()


def print_roi_block(rows, label="ALL"):
    if not rows:
        return
    roi_values = [d["roi"] * 100 for d in rows]
    profit_values = [d.get("profit_rub", 0) for d in rows]
    print(sep("-"))
    print(f"  Calculated signal ROI ({label}, n={len(roi_values)})")
    print(sep("-"))
    print(f"    median  {S.median(roi_values):7.2f}%")
    print(f"    mean    {S.mean(roi_values):7.2f}%")
    print(f"    min     {min(roi_values):7.2f}%")
    print(f"    max     {max(roi_values):7.2f}%")
    print(f"    >=5%:   {sum(1 for r in roi_values if r >= 5):4d}   "
          f">=10%: {sum(1 for r in roi_values if r >= 10):4d}   "
          f">=20%: {sum(1 for r in roi_values if r >= 20):4d}   "
          f">=35%: {sum(1 for r in roi_values if r >= 35):4d}")
    print()
    print("  Calculated payout surplus per signal (RUB; not realized P&L)")
    print(f"    median  {S.median(profit_values):8.0f}")
    print(f"    mean    {S.mean(profit_values):8.0f}")
    print(f"    max     {max(profit_values):8.0f}")
    print()


def print_grouped(rows, key, title):
    groups = C.defaultdict(list)
    for row in rows:
        groups[row.get(key, "?")].append(row["roi"] * 100)
    print(sep("-"))
    print(f"  Median signal ROI by {title}")
    print(sep("-"))
    print(f"    {'key':<12}{'n':>6}{'median':>10}{'mean':>10}{'max':>10}")
    for key_value, values in sorted(groups.items(), key=lambda item: -len(item[1])):
        print(f"    {key_value:<12}{len(values):>6}{S.median(values):>9.2f}%"
              f"{S.mean(values):>9.2f}%{max(values):>9.2f}%")
    print()


def print_top(rows, n=10):
    print(sep("-"))
    print(f"  Top-{n} signal snapshots by ROI")
    print(sep("-"))
    best = sorted(rows, key=lambda row: -row["roi"])[:n]
    for row in best:
        print("    %+6.1f%%  %-8s %-8s  %-42s  estimate %5.0fR"
              % (row["roi"] * 100, row.get("sport", "?"),
                 row.get("market_type", "?"), row["slug"][:42],
                 row.get("profit_rub", 0)))
    print()


def print_persistence(rows, n=15):
    counts = C.Counter(d["slug"] for d in rows)
    print(sep("-"))
    print(f"  Most frequently observed signal slugs (top {n})")
    print(sep("-"))
    for slug, count in counts.most_common(n):
        best_roi = max((d["roi"] * 100 for d in rows if d["slug"] == slug), default=0)
        print(f"    {count:>4}x  {slug[:50]:<50}  best {best_roi:+6.1f}%")
    print()


def dedup_best_per_slug(rows):
    """Select the highest observed signal per slug; this does not imply a fill."""
    best = {}
    for row in rows:
        slug = row.get("slug")
        if not slug:
            continue
        if slug not in best or row["roi"] > best[slug]["roi"]:
            best[slug] = row
    return list(best.values())


def print_honest_estimate(rows):
    best = dedup_best_per_slug(rows)
    if not best:
        return
    roi_values = [d["roi"] * 100 for d in best]
    profit_values = [d.get("profit_rub", 0) for d in best]
    print(sep("-"))
    print(f"  Best observed snapshot per slug (n={len(best)}; not executed trades)")
    print(sep("-"))
    print(f"    ROI median                 {S.median(roi_values):.2f}%")
    print(f"    ROI mean                   {S.mean(roi_values):.2f}%")
    print(f"    Calculated surplus median  {S.median(profit_values):.0f} RUB")
    print(f"    Sum of displayed estimates {sum(profit_values):.0f} RUB")
    print("    These are theoretical signal snapshots, not realized profit.")
    print()


def _csv_safe(value):
    if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@", "\t", "\r")):
        return "'" + value
    return value


def export_csv(rows, path):
    best = dedup_best_per_slug(rows)
    best.sort(key=lambda row: -row["roi"])
    columns = ["ts", "slug", "sport", "market_type", "live", "streamer",
               "roi", "bank_rub", "profit_rub", "payout_rub", "rate_usdrub"]
    with open(path, "w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(columns)
        for row in best:
            writer.writerow([_csv_safe(row.get(column, "")) for column in columns])
    print(f"[*] wrote {len(best)} rows -> {path}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--file", default="arbs.jsonl")
    parser.add_argument("--csv", default="")
    parser.add_argument("--since", default="")
    parser.add_argument("--min-roi", type=float, default=None)
    parser.add_argument("--sport", default="")
    parser.add_argument("--type", default="")
    args = parser.parse_args()

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
