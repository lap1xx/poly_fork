#!/usr/bin/env python3
# scan_live.py v3 - colors + only-low filter
import argparse
import json
import os
import sys
import time
import requests

if sys.platform == "win32":
	os.system("")

COLON = chr(58)
SLASH = chr(47)
DOT = chr(46)
HT = "https" + COLON + SLASH + SLASH
GAMMA = HT + "gamma-api" + DOT + "polymarket" + DOT + "com"
CLOB = HT + "clob" + DOT + "polymarket" + DOT + "com"
TIMEOUT = 15.0

class C:
	G = chr(27) + "[92m"
	R = chr(27) + "[91m"
	Y = chr(27) + "[93m"
	B = chr(27) + "[94m"
	M = chr(27) + "[95m"
	CY = chr(27) + "[96m"
	W = chr(27) + "[97m"
	DIM = chr(27) + "[2m"
	BOLD = chr(27) + "[1m"
	RST = chr(27) + "[0m"

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

SPORT_COLOR = {
	"CS2": C.G,
	"DOTA2": C.M,
	"LOL": C.CY,
	"VALORANT": C.R,
	"R6": C.B,
	"ROCKET": C.B,
	"TENNIS": C.B,
}

LOW_LEAGUES = ["cct", "esea-open", "esea-intermediate", "esea-advanced", "esea-md", "united21", "nodwin", "elisa", "challenger", "qualifier", "open-qualifier", "closed-qualifier", "european-pro-league", "cis-lan", "academy", "youth", "junior", "reserves", "w15", "w25", "w40", "w60", "w75", "w100", "itf", "m15", "m25", "winline-insight", "winline-star", "beast-cup", "fantasy-cup", "gamers-club", "esports-battle", "cups", "show-cup", "boom-cup", "rising-stars", "talent", "development", "amateur"]
STREAMER_KEYS = ["streamer", "showmatch", "show-match", "influencer", "charity", "pro-am", "proam", "star-series", "star series", "esforce", "betboom", "winline", "parimatch", "1win", "boom-cup", "cyber-battle", "esports-battle", "streamers-battle", "streamers cup", "media-tournament", "blitz-cup", "show-cup", "beast-cup", "kick"]

def sport_of(slug):
	s = slug.lower()
	for p, n in SPORTS:
		if s.startswith(p):
			return n
	return ""

def is_low_league(slug, q):
	s = (slug + " " + q).lower()
	for k in LOW_LEAGUES:
		if k in s:
			return True
	return False

def is_streamer(slug, q):
	s = (slug + " " + q).lower()
	for k in STREAMER_KEYS:
		if k in s:
			return True
	return False

def color_sport(sport):
	return SPORT_COLOR.get(sport, C.W)

def color_over(over):
	if over <= 1.005:
		return C.G
	if over <= 1.02:
		return C.Y
	return C.R

def color_liq(liq):
	if liq >= 5000:
		return C.G
	if liq >= 500:
		return C.Y
	return C.R

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
	try:
		tokens = json.loads(m.get("clobTokenIds", "[]"))
	except Exception:
		return None
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
	if liq0 < 20 and liq1 < 20:
		return None
	over = p0 + p1
	return {"slug": m.get("slug", ""), "q": m.get("question", ""), "p0": p0, "s0": s0, "liq0": liq0, "p1": p1, "s1": s1, "liq1": liq1, "over": over}

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
	ap.add_argument("--only-low", action="store_true")
	ap.add_argument("--min-liq", type=float, default=50)
	args = ap.parse_args()

	print(C.BOLD + C.B + "[*] fetching live events..." + C.RST)
	events = fetch_live_events()
	print(C.DIM + "[*] got " + str(len(events)) + " raw events" + C.RST)

	filtered = [e for e in events if sport_of(e.get("slug", "")) != ""]
	print("[*] " + str(len(filtered)) + " esports/tennis events")

	if args.sport:
		filtered = [e for e in filtered if sport_of(e.get("slug", "")).upper() == args.sport.upper()]
		print("[*] after --sport: " + str(len(filtered)))

	if args.only_low:
		filtered = [e for e in filtered if is_low_league(e.get("slug", ""), e.get("title", ""))]
		print(C.Y + "[*] after --only-low: " + str(len(filtered)) + C.RST)

	for ev in filtered:
		slug = ev.get("slug", "")
		sport = sport_of(slug)
		title = ev.get("title", "")
		score = ev.get("score", "")
		period = ev.get("period", "")
		sc = color_sport(sport)
		tier = "LOW" if is_low_league(slug, title) else "MID"
		tier_col = C.Y if tier == "LOW" else C.W
		streamer = is_streamer(slug, title)
		streamer_tag = (C.M + " [STREAMER]" + C.RST) if streamer else ""
		markets = ev.get("markets", []) or []

		summaries = [market_summary(m) for m in markets]
		summaries = [s for s in summaries if s is not None]
		summaries.sort(key=lambda x: x["over"])

		print()
		print(C.BOLD + "=" * 95 + C.RST)
		print(sc + C.BOLD + "[" + sport + "]" + C.RST + " " + C.BOLD + title + C.RST + streamer_tag)
		print(C.DIM + "  tier: " + C.RST + tier_col + tier + C.RST + C.DIM + "   score: " + str(score) + "   period: " + str(period) + C.RST)
		print(C.DIM + "  slug: " + slug + C.RST)
		print(C.DIM + "  markets with liquidity: " + str(len(summaries)) + " / " + str(len(markets)) + C.RST)
		if len(summaries) == 0:
			print(C.DIM + "    (no liquid markets right now)" + C.RST)
			continue
		for s in summaries:
			tag = "MAIN" if is_main(s["slug"], s["q"]) else "SUB"
			tag_col = C.G if tag == "MAIN" else C.Y
			over_col = color_over(s["over"])
			liq0_col = color_liq(s["liq0"])
			liq1_col = color_liq(s["liq1"])
			print()
			print("  " + tag_col + "[" + tag + "]" + C.RST + " over=" + over_col + format(s["over"], ".3f") + C.RST + "  " + s["q"][:80])
			print(C.DIM + "    slug: " + s["slug"] + C.RST)
			print("    side A: ask=" + format(s["p0"], ".3f") + "  k=" + format((1.0/s["p0"]) if s["p0"] > 0 else 0, ".2f") + "  liq=" + liq0_col + "USD" + format(s["liq0"], ",.0f") + C.RST)
			print("    side B: ask=" + format(s["p1"], ".3f") + "  k=" + format((1.0/s["p1"]) if s["p1"] > 0 else 0, ".2f") + "  liq=" + liq1_col + "USD" + format(s["liq1"], ",.0f") + C.RST)

	print()
	print(C.BOLD + "[+] done. " + str(len(filtered)) + " live events" + C.RST)


try:
	main()
except KeyboardInterrupt:
	print()
	print("interrupted")
