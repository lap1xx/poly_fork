#!/usr/bin/env python3
# scan_live.py - find live esports + tennis matches on Polymarket
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
NL = chr(10)

SPORT_PREFIXES = [
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

MAP_KEYWORDS = ["game2", "game3", "game4", "game5", "set2", "set3", "set4", "set5", "map2", "map3", "o/u", "total"]


def sport_of(slug):
	s = slug.lower()
	for p, name in SPORT_PREFIXES:
		if s.startswith(p):
			return name
	return ""


def req_retry(url, params=None, tries=3):
	for attempt in range(tries):
		try:
			r = requests.get(url, params=params, timeout=TIMEOUT)
			r.raise_for_status()
			return r
		except Exception as e:
			if "404" in str(e):
				return None
			time.sleep(2 + attempt)
	return None


def fetch_events_live():
	params = {"active": "true", "closed": "false", "limit": 200, "live": "true"}
	r = req_retry(GAMMA + "/events", params=params)
	if not r:
		return []
	try:
		return r.json()
	except Exception:
		return []


def fetch_events_all():
	params = {"active": "true", "closed": "false", "limit": 300}
	r = req_retry(GAMMA + "/events", params=params)
	if not r:
		return []
	try:
		return r.json()
	except Exception:
		return []


def is_map_market(slug, question):
	low = (slug + " " + question).lower()
	return any(k in low for k in MAP_KEYWORDS)


def best_ask(tid):
	r = req_retry(CLOB + "/book", params={"token_id": tid}, tries=2)
	if not r:
		return None
	try:
		book = r.json()
	except Exception:
		return None
	asks = book.get("asks") or []
	if not asks:
		return (0.0, 0.0)
	best = min(asks, key=lambda x: float(x["price"]))
	return (float(best["price"]), float(best["size"]))


def main():
	ap = argparse.ArgumentParser(prog="scan_live")
	ap.add_argument("--sport", default="")
	ap.add_argument("--show-all", action="store_true")
	args = ap.parse_args()

	print("[*] trying /events?live=true ...")
	events = fetch_events_live()
	print("[*] got " + str(len(events)) + " from live filter")

	if len(events) == 0:
		print("[*] empty, trying full events list and filtering...")
		events_all = fetch_events_all()
		events = [e for e in events_all if e.get("live") is True]
		print("[*] got " + str(len(events)) + " live events after filter")

	filtered = [e for e in events if sport_of(e.get("slug", "")) != ""]
	print("[*] " + str(len(filtered)) + " esports/tennis live events")

	if args.sport:
		filtered = [e for e in filtered if sport_of(e.get("slug", "")).upper() == args.sport.upper()]
		print("[*] after --sport filter: " + str(len(filtered)))

	if len(filtered) == 0:
		print()
		print("[!] no live esports/tennis right now.")
		print("    run this again in a few minutes, or use prematch scanner:")
		print("    python scan_2way.py --sport CS2")
		return

	for ev in filtered:
		slug = ev.get("slug", "")
		sport = sport_of(slug)
		title = ev.get("title", "")
		score = ev.get("score", "")
		period = ev.get("period", "")
		series = ev.get("seriesSlug", "")
		print()
		print("=" * 100)
		print("[" + sport + "] " + title)
		print("  slug:   " + slug)
		print("  score:  " + str(score) + "   period: " + str(period) + "   series: " + series)
		markets = ev.get("markets", []) or []
		map_markets = [m for m in markets if is_map_market(m.get("slug", ""), m.get("question", ""))]
		main_markets = [m for m in markets if not is_map_market(m.get("slug", ""), m.get("question", ""))]
		print("  markets: " + str(len(markets)) + "  (map/set: " + str(len(map_markets)) + ", main: " + str(len(main_markets)) + ")")
		for m in markets:
			mslug = m.get("slug", "")
			mq = m.get("question", "")
			live_m = m.get("live", False)
			ended = m.get("ended", False)
			closed = m.get("closed", False)
			tag = "MAP/SET" if is_map_market(mslug, mq) else "MAIN"
			status = "LIVE" if live_m else ("ENDED" if ended else ("CLOSED" if closed else "ACTIVE"))
			tokens_raw = m.get("clobTokenIds", "[]")
			try:
				tokens = json.loads(tokens_raw)
			except Exception:
				tokens = []
			asks = []
			for t in tokens:
				res = best_ask(t)
				asks.append(res if res else (0.0, 0.0))
			print("    [" + tag.ljust(7) + "/" + status.ljust(6) + "] " + mq[:75])
			print("      slug: " + mslug)
			for i, t in enumerate(tokens):
				a, s = asks[i]
				k = (1.0 / a) if a > 0 else 0.0
				print("        token" + str(i) + ": ask=" + format(a, ".3f") + "  k=" + format(k, ".2f") + "  size=" + format(s, ",.0f"))

	print()
	print("[+] done. " + str(len(filtered)) + " live events")
	print("[*] to monitor one of them:")
	print("    python polyarb_v3.py --slugs " + (filtered[0].get("slug", "") if len(filtered) > 0 else "<slug>"))


main()
