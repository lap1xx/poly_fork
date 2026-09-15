#!/usr/bin/env python3
# scan_live.py v2 - clean output, only markets with liquidity
import argparse
import json
import sys
import time
import requests

COLON = chr(58)
SLASH = chr(47)
DOT = chr(46)
HT = "http" + chr(115) + COLON + SLASH + SLASH
GAMMA = HT + "gamma-api" + DOT + "polymarket" + DOT + "com"
CLOB = HT + "clob" + DOT + "polymarket" + DOT + "com"
TIMEOUT = 15.0

SPORTS = [
	("cs2-", "CS2"),
	("dota2-", "DOTA2"),
	("lol-", "LOL"),
	("valorant-", "VALORANT"),
	("r6-", "R6"),
	("rl-", "ROCKET"),
	("atp-", "TENNIS"),
	("wta-", "TENNIS"),
	("itf-", "TENNIS"),
]


def sport_of(slug):
	s = slug.lower()
	for p, n in SPORTS:
		if s.startswith(p):
			return n
	return ""


def req_retry(url, params=None, tries=2):
	for a in range(tries):
		try:
			r = requests.get(url, params=params, timeout=TIMEOUT)
			if r.status_code == 404:
				return None
			r.raise_for_status()
			return r
		except Exception as e:
			if "404" in str(e):
				return None
			time.sleep(1)
	return None


def fetch_live_events():
	params = {"active": "true", "closed": "false", "limit": 200, "live": "true"}
	r = req_retry(GAMMA + "/events", params=params)
	if not r:
		return []
	try:
		return r.json()
	except Exception:
		return []


def best_ask(tid):
	r = req_retry(CLOB + "/book", params={"token_id": tid})
	if not r:
		return None
	try:
		book = r.json()
	except Exception:
		return None
	asks = book.get("asks") or []
	if not asks:
		return (0.0, 0.0)
	b = min(asks, key=lambda x: float(x["price"]))
	return (float(b["price"]), float(b["size"]))


def market_summary(m):
	tokens_raw = m.get("clobTokenIds", "[]")
	try:
		tokens = json.loads(tokens_raw)
	except Exception:
		tokens = []
	if len(tokens) != 2:
		return None
	a0 = best_ask(tokens[0])
	a1 = best_ask(tokens[1])
	if a0 is None or a1 is None:
		return None
	p0, s0 = a0
	p1, s1 = a1
	liq0 = p0 * s0
	liq1 = p1 * s1
	# skip markets where neither side has real liquidity
	if liq0 < 20 and liq1 < 20:
		return None
	over = p0 + p1
	return {
		"slug": m.get("slug", ""),
		"q": m.get("question", ""),
		"p0": p0, "s0": s0, "liq0": liq0,
		"p1": p1, "s1": s1, "liq1": liq1,
		"over": over,
		"live": m.get("live", False),
		"closed": m.get("closed", False),
		"ended": m.get("ended", False),
	}


def is_main(slug, q):
	low = (slug + " " + q).lower()
	bad = ["game", "map", "set ", "set-", "handicap", "spread", "o/u", "total", "round"]
	for b in bad:
		if b in low:
			return False
	return True


def main():
	ap = argparse.ArgumentParser(prog="scan_live")
	ap.add_argument("--sport", default="")
	ap.add_argument("--min-liq", type=float, default=50)
	args = ap.parse_args()

	print("[*] fetching live events...")
	events = fetch_live_events()
	print("[*] got " + str(len(events)) + " raw events")

	filtered = [e for e in events if sport_of(e.get("slug", "")) != ""]
	print("[*] " + str(len(filtered)) + " esports/tennis events")

	if args.sport:
		filtered = [e for e in filtered if sport_of(e.get("slug", "")).upper() == args.sport.upper()]
		print("[*] after --sport: " + str(len(filtered)))

	for ev in filtered:
		slug = ev.get("slug", "")
		sport = sport_of(slug)
		title = ev.get("title", "")
		score = ev.get("score", "")
		period = ev.get("period", "")
		markets = ev.get("markets", []) or []

		summaries = [market_summary(m) for m in markets]
		summaries = [s for s in summaries if s is not None]
		# sort by overround ascending (tightest first)
		summaries.sort(key=lambda x: x["over"])

		print()
		print("=" * 95)
		print("[" + sport + "] " + title + "   score: " + str(score) + "  period: " + str(period))
		print("  event slug: " + slug)
		print("  markets with liquidity: " + str(len(summaries)) + " / " + str(len(markets)))
		if len(summaries) == 0:
			print("    (no liquid markets right now)")
			continue
		for s in summaries:
			tag = "MAIN" if is_main(s["slug"], s["q"]) else "SUB"
			print()
			print("  [" + tag + "] over=" + format(s["over"], ".3f") + "  " + s["q"][:80])
			print("    slug: " + s["slug"])
			print("    side A: ask=" + format(s["p0"], ".3f") + "  k=" + format((1.0/s["p0"]) if s["p0"] > 0 else 0, ".2f") + "  liq=" + format(s["liq0"], ",.0f"))
			print("    side B: ask=" + format(s["p1"], ".3f") + "  k=" + format((1.0/s["p1"]) if s["p1"] > 0 else 0, ".2f") + "  liq=" + format(s["liq1"], ",.0f"))

	print()
	print("[+] done. " + str(len(filtered)) + " live events")


try:
	main()
except KeyboardInterrupt:
	print()
	print("interrupted by user")
