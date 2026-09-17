#!/usr/bin/env python3
# scan_2way.py v10 - LOW tiers + streamers + extended BK
import argparse
import concurrent.futures
import json
import os
import sys
import time
from datetime import datetime, timezone, timedelta
import requests

COLON = chr(58)
SLASH = chr(47)
DOT = chr(46)
HT = "https" + COLON + SLASH + SLASH
GAMMA = HT + "gamma-api" + DOT + "polymarket" + DOT + "com"
CLOB = HT + "clob" + DOT + "polymarket" + DOT + "com"
TIMEOUT = 15.0
NL = chr(10)
PAGE = 100
CFG_FILE = "config.json"
PREV_FILE = "2way_prev.json"
CACHE = {}
CACHE_TTL = 10.0

SPORT_PREFIXES = [
	("cs2-", "CS2"), ("dota2-", "DOTA2"), ("lol-", "LOL"), ("valorant-", "VALORANT"),
	("atp-", "TENNIS"), ("wta-", "TENNIS"), ("itf-", "TENNIS"),
]

SPORT_WEIGHT = {
	"CS2": 1.0, "DOTA2": 1.0, "LOL": 0.9, "VALORANT": 0.85,
	"R6": 0.75, "ROCKET": 0.75, "COD": 0.65, "PUBG": 0.65,
	"APEX": 0.55, "OW": 0.55, "SC2": 0.55, "MLBB": 0.55,
	"FIFA": 0.55, "EAFC": 0.55, "TENNIS": 0.85,
}

STREAMER_KEYS = ["streamer", "showmatch", "show-match", "show match", "influencer", "charity", "pro-am", "proam", "star-series", "star series", "esforce", "betboom", "winline", "parimatch", "1win", "content-creator", "vs-streamer", "creator", "youtube", "twitch", "kick", "boom-cup", "cyber-battle", "esports-battle", "streamers-battle", "streamers cup", "media-tournament", "media tournament", "blitz-cup", "show-cup", "beast-cup", "showmatch-cup"]

LOW_LEAGUES = ["cct", "esea-open", "esea-intermediate", "esea-advanced", "esea-md", "united21", "nodwin", "elisa", "challenger", "qualifier", "open-qualifier", "closed-qualifier", "european-pro-league", "cis-lan", "academy", "youth", "junior", "reserves", "w15", "w25", "w40", "w60", "w75", "w100", "itf", "m15", "m25", "winline-insight", "winline-star", "beast-cup", "fantasy-cup", "gamers-club", "esports-battle", "cups", "show-cup", "boom-cup", "rising-stars", "talent", "development", "amateur"]

SUFFIXES = ["-game1", "-game2", "-game3", "-game4", "-game5", "-set2", "-set3", "-set4", "-set5", "-first-set-winner", "-set-2-winner", "-set-3-winner"]
INFIXES = ["-total-", "-set-totals-", "-spread-", "-handicap-", "-round-total-", "-round-handicap-", "-game-handicap-", "-set-handicap-", "-completed-match"]

def load_cfg():
	try:
		f = open(CFG_FILE, encoding="utf-8")
		c = json.load(f)
		f.close()
		return c
	except Exception:
		return {}

def sport_of(slug):
	s = slug.lower()
	for p, n in SPORT_PREFIXES:
		if s.startswith(p):
			return n
	return ""

def base_slug(slug):
	s = slug
	for suf in SUFFIXES:
		if s.endswith(suf):
			return s[:-len(suf)]
	for inf in INFIXES:
		if inf in s:
			return s.split(inf)[0]
	return s

def is_streamer(slug, q):
	s = (slug + " " + q).lower()
	for k in STREAMER_KEYS:
		if k in s:
			return True
	return False

def is_low_league(slug, q):
	s = (slug + " " + q).lower()
	for k in LOW_LEAGUES:
		if k in s:
			return True
	return False

def tier_from_league(m):
	if is_low_league(m.get("slug", ""), m.get("question", "")):
		return "LOW"
	try:
		em = m.get("eventMetadata") or {}
		t = int(em.get("leagueTier", 0))
	except Exception:
		t = 0
	if t == 0:
		return "MID"
	if t <= 2:
		return "TOP"
	if t <= 4:
		return "MID"
	return "LOW"

def is_future(m):
	if m.get("ended") is True:
		return False
	if m.get("closed") is True:
		return False
	iso = m.get("endDate") or ""
	if not iso:
		return True
	try:
		s = iso.replace("Z", "+00:00")
		dt = datetime.fromisoformat(s)
		now = datetime.now(timezone.utc)
		return dt > now
	except Exception:
		return True

def start_within_hours(m, hours):
	if hours <= 0:
		return True
	iso = m.get("eventStartTime") or m.get("gameStartTime") or ""
	if not iso:
		return True
	try:
		s = iso.replace("Z", "+00:00").replace(" ", "T")
		dt = datetime.fromisoformat(s)
		now = datetime.now(timezone.utc)
		return (dt - now) < timedelta(hours=hours)
	except Exception:
		return True

def req_retry(url, params=None, tries=3):
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
			time.sleep(1 + a)
	return None

def fetch_page(offset, limit):
	params = {"active": "true", "closed": "false", "limit": limit, "offset": offset, "order": "volume24hr", "ascending": "false"}
	r = req_retry(GAMMA + "/markets", params=params)
	return r.json() if r else []

def fetch_all(max_markets):
	all_m = []
	offset = 0
	fails = 0
	while len(all_m) < max_markets:
		take = min(PAGE, max_markets - len(all_m))
		page = fetch_page(offset, take)
		if not page:
			fails += 1
			if fails >= 3:
				break
			offset += PAGE
			time.sleep(3)
			continue
		fails = 0
		all_m.extend(page)
		offset += len(page)
		print("    fetched " + str(len(all_m)))
		if len(page) < take:
			break
		time.sleep(0.4)
	return all_m

def best_ask(tid):
	now = time.time()
	if tid in CACHE:
		ts, a, s = CACHE[tid]
		if now - ts < CACHE_TTL:
			return (a, s)
	r = req_retry(CLOB + "/book", params={"token_id": tid}, tries=2)
	if not r:
		return None
	try:
		book = r.json()
	except Exception:
		return None
	asks = book.get("asks") or []
	res = (0.0, 0.0)
	if asks:
		b = min(asks, key=lambda x: float(x["price"]))
		res = (float(b["price"]), float(b["size"]))
	CACHE[tid] = (now, res[0], res[1])
	return res

def analyze(m, min_notional):
	try:
		outcomes = json.loads(m["outcomes"])
		tokens = json.loads(m["clobTokenIds"])
	except Exception:
		return "parse"
	if len(outcomes) != 2:
		return "outcomes"
	asks = []
	sizes = []
	for t in tokens:
		res = best_ask(t)
		if res is None:
			return "api"
		asks.append(res[0])
		sizes.append(res[1])
	if any(a <= 0 for a in asks):
		return "empty_ask"
	n0 = asks[0] * sizes[0]
	n1 = asks[1] * sizes[1]
	if n0 < min_notional or n1 < min_notional:
		return "low_liq"
	k0 = 1.0 / asks[0]
	k1 = 1.0 / asks[1]
	if k0 > 7.0 or k1 > 7.0 or k0 < 1.15 or k1 < 1.15:
		return "extreme_odds"
	return {"slug": m["slug"], "q": m.get("question", ""), "o": outcomes, "asks": asks, "sizes": sizes, "notional": [n0, n1], "over": sum(asks), "vol": float(m.get("volume24hr") or 0), "end": m.get("endDate", ""), "start": m.get("eventStartTime", "") or m.get("gameStartTime", ""), "league": (m.get("eventMetadata") or {}).get("league", "")}

def load_prev():
	try:
		f = open(PREV_FILE, encoding="utf-8")
		d = json.load(f)
		f.close()
		return set(d.get("base_slugs", []))
	except Exception:
		return set()

def save_prev(base_slugs):
	try:
		f = open(PREV_FILE, "w", encoding="utf-8")
		json.dump({"base_slugs": sorted(base_slugs), "ts": time.time()}, f)
		f.close()
	except Exception:
		pass

def main():
	ap = argparse.ArgumentParser(prog="scan_2way")
	ap.add_argument("--top", type=int, default=800)
	ap.add_argument("--show", type=int, default=40)
	ap.add_argument("--min-volume", type=float, default=100)
	ap.add_argument("--min-notional", type=float, default=50)
	ap.add_argument("--max-overround", type=float, default=1.08)
	ap.add_argument("--sport", default="")
	ap.add_argument("--tier", default="")
	ap.add_argument("--only-low", action="store_true")
	ap.add_argument("--hours", type=float, default=0.0)
	ap.add_argument("--workers", type=int, default=10)
	args = ap.parse_args()

	cfg = load_cfg()
	sc = cfg.get("scan", {})
	top = args.top or sc.get("top", 800)
	show = args.show or sc.get("show", 40)
	min_vol = args.min_volume or sc.get("min_volume", 100)
	min_notional = args.min_notional or sc.get("min_notional", 50)
	max_over = args.max_overround or sc.get("max_overround", 1.08)
	sport = args.sport or sc.get("sport", "")
	tier = args.tier or sc.get("tier", "")
	hours = args.hours

	print("[*] top=" + str(top) + " show=" + str(show) + " hours=" + str(hours) + " only_low=" + str(args.only_low))
	print("[*] fetching " + str(top) + " markets...")
	markets = fetch_all(top)
	print("[*] got " + str(len(markets)) + " raw markets")

	filtered = []
	for m in markets:
		slug = m.get("slug", "")
		try:
			oc = json.loads(m.get("outcomes", "[]"))
		except Exception:
			continue
		if len(oc) != 2:
			continue
		if float(m.get("volume24hr") or 0) < min_vol:
			continue
		if sport_of(slug) == "":
			continue
		if not is_future(m):
			continue
		if not start_within_hours(m, hours):
			continue
		filtered.append(m)

	print("[*] " + str(len(filtered)) + " candidates after pre-filter")

	by_sport = {}
	for m in filtered:
		s = sport_of(m.get("slug", ""))
		by_sport[s] = by_sport.get(s, 0) + 1
	for k in sorted(by_sport.keys()):
		print("      " + k.ljust(10) + " " + str(by_sport[k]))

	results = []
	reasons = {}
	with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as ex:
		futures = {ex.submit(analyze, m, min_notional): m for m in filtered}
		done = 0
		for fut in concurrent.futures.as_completed(futures):
			done += 1
			m = futures[fut]
			try:
				r = fut.result()
			except Exception:
				r = "api"
			if isinstance(r, str):
				reasons[r] = reasons.get(r, 0) + 1
			else:
				r["sport"] = sport_of(r["slug"])
				r["tier"] = tier_from_league(m)
				r["weight"] = SPORT_WEIGHT.get(r["sport"], 0.5)
				r["score"] = r["over"] / r["weight"]
				r["streamer"] = is_streamer(r["slug"], r["q"])
				results.append(r)
			if done % 20 == 0:
				print("    done " + str(done) + " / " + str(len(filtered)))

	print("[*] rejection reasons:")
	for k in sorted(reasons.keys()):
		print("      " + k.ljust(15) + " " + str(reasons[k]))

	if sport:
		results = [r for r in results if r["sport"].upper() == sport.upper()]
	if tier:
		results = [r for r in results if r["tier"].upper() == tier.upper()]
	if args.only_low:
		results = [r for r in results if r["tier"].upper() == "LOW"]

	prev = load_prev()
	new_slugs = set()
	for r in results:
		bs = base_slug(r["slug"])
		if bs not in prev:
			r["is_new"] = True
			new_slugs.add(bs)
	save_prev(new_slugs | prev)

	results.sort(key=lambda x: (x["score"], x["over"]))
	good = [r for r in results if r["over"] <= max_over]

	print()
	print("=" * 115)
	print("Top " + str(show) + " - over<=" + str(max_over) + " notional>=USD" + str(int(min_notional)) + " hours<=" + str(hours))
	print("=" * 115)

	for i, r in enumerate(good[:show], 1):
		print()
		marker = " [NEW] " if r.get("is_new") else "       "
		streamer_tag = " [STREAMER]" if r.get("streamer") else ""
		badge = "[" + r["sport"].ljust(8) + "/" + r["tier"].ljust(3) + "]"
		print("#" + str(i).ljust(3) + marker + badge + " over=" + format(r["over"], ".4f") + " score=" + format(r["score"], ".4f") + " vol=" + format(r["vol"], ",.0f") + streamer_tag)
		print("     " + r["q"][:95])
		print("     start: " + r["start"] + "   slug: " + r["slug"])
		for oc, a, s, n in zip(r["o"], r["asks"], r["sizes"], r["notional"]):
			print("       " + oc[:28].ljust(28) + " ask=" + format(a, ".3f") + "  k=" + format(1.0/a, ".2f") + "  USDliq=" + format(n, ",.0f"))

	print()
	print("[+] analyzed " + str(len(results)) + ", within: " + str(len(good)) + ", new: " + str(len(new_slugs)))

	f = open("2way_candidates.txt", "w", encoding="utf-8")
	f.write("sport	tier	market	overround	score	volume24h	league	start	end	slug	question" + NL)
	for r in results:
		line = r["sport"] + "	" + r["tier"] + "	MATCH	"
		line += format(r["over"], ".4f") + "	" + format(r["score"], ".4f") + "	"
		line += format(r["vol"], ".0f") + "	" + r["league"] + "	"
		line += r["start"] + "	" + r["end"] + "	"
		line += r["slug"] + "	" + r["q"].replace("	", " ")
		f.write(line + NL)
	f.close()
	print("[+] saved 2way_candidates.txt")

try:
	main()
except KeyboardInterrupt:
	print()
	print("interrupted")
