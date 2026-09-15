#!/usr/bin/env python3
# scan_2way.py v8 - esports + tennis only
import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
import requests

COLON = chr(58)
SLASH = chr(47)
DOT = chr(46)
HT = "http" + chr(115) + COLON + SLASH + SLASH
GAMMA = HT + "gamma-api" + DOT + "polymarket" + DOT + "com"
CLOB = HT + "clob" + DOT + "polymarket" + DOT + "com"
TIMEOUT = 15.0
NL = chr(10)
PAGE = 100
CFG_FILE = "config.json"

# only esports + tennis
SPORT_PREFIXES = [
	("cs2-", "CS2"),
	("dota2-", "DOTA2"),
	("lol-", "LOL"),
	("valorant-", "VALORANT"),
	("r6-", "R6"),
	("rl-", "ROCKET"),
	("cod-", "COD"),
	("pubg-", "PUBG"),
	("apex-", "APEX"),
	("ow-", "OW"),
	("sc2-", "SC2"),
	("mlbb-", "MLBB"),
	("fifa-", "FIFA"),
	("fc-", "EAFC"),
	("atp-", "TENNIS"),
	("wta-", "TENNIS"),
	("itf-", "TENNIS"),
]

DEFAULT_CFG = {
	"bank": 20000,
	"rate": 0,
	"scan": {
		"top": 800,
		"show": 40,
		"min_volume": 200,
		"min_notional": 50,
		"max_overround": 1.08,
		"sport": "",
		"tier": "",
	},
	"bot": {
		"poll": 1.0,
		"slip": 0.0,
	},
}


def load_cfg():
	if not os.path.exists(CFG_FILE):
		f = open(CFG_FILE, "w", encoding="utf-8")
		json.dump(DEFAULT_CFG, f, indent=2, ensure_ascii=False)
		f.close()
		return dict(DEFAULT_CFG)
	try:
		f = open(CFG_FILE, encoding="utf-8")
		c = json.load(f)
		f.close()
		out = dict(DEFAULT_CFG)
		out.update(c)
		return out
	except Exception:
		return dict(DEFAULT_CFG)


def sport_of(slug):
	s = slug.lower()
	for p, name in SPORT_PREFIXES:
		if s.startswith(p):
			return name
	return ""


def tier_from_league(m):
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


def req_with_retry(url, params=None, tries=3):
	for attempt in range(tries):
		try:
			r = requests.get(url, params=params, timeout=TIMEOUT)
			r.raise_for_status()
			return r
		except Exception:
			time.sleep(2 + attempt * 1)
	return None


def fetch_page(offset, limit):
	params = {"active": "true", "closed": "false", "limit": limit,
		"offset": offset, "order": "volume24hr", "ascending": "false"}
	r = req_with_retry(GAMMA + "/markets", params=params)
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
		time.sleep(0.6)
	return all_m


def best_ask(tid):
	r = req_with_retry(CLOB + "/book", params={"token_id": tid}, tries=2)
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
		a, s = res
		asks.append(a)
		sizes.append(s)
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
	return {
		"slug": m["slug"],
		"q": m.get("question", ""),
		"o": outcomes,
		"asks": asks,
		"sizes": sizes,
		"notional": [n0, n1],
		"over": sum(asks),
		"vol": float(m.get("volume24hr") or 0),
		"end": m.get("endDate", ""),
		"start": m.get("eventStartTime", "") or m.get("gameStartTime", ""),
		"league": (m.get("eventMetadata") or {}).get("league", ""),
	}


def main():
	ap = argparse.ArgumentParser(prog="scan_2way")
	ap.add_argument("--top", type=int, default=0)
	ap.add_argument("--show", type=int, default=0)
	ap.add_argument("--min-volume", type=float, default=0.0)
	ap.add_argument("--min-notional", type=float, default=0.0)
	ap.add_argument("--max-overround", type=float, default=0.0)
	ap.add_argument("--sport", default="")
	ap.add_argument("--tier", default="")
	args = ap.parse_args()

	cfg = load_cfg()
	sc = cfg.get("scan", {})
	top = args.top if args.top > 0 else sc.get("top", 800)
	show = args.show if args.show > 0 else sc.get("show", 40)
	min_vol = args.min_volume if args.min_volume > 0 else sc.get("min_volume", 200)
	min_notional = args.min_notional if args.min_notional > 0 else sc.get("min_notional", 200)
	max_over = args.max_overround if args.max_overround > 0 else sc.get("max_overround", 1.08)
	sport = args.sport if args.sport else sc.get("sport", "")
	tier = args.tier if args.tier else sc.get("tier", "")

	print("[*] cfg: top=" + str(top) + " show=" + str(show) + " min_notional=" + str(min_notional) + " max_over=" + str(max_over) + " sport=" + (sport or "ESPORTS+TENNIS"))
	print("[*] fetching up to " + str(top) + " active markets...")
	markets = fetch_all(top)
	print("[*] got " + str(len(markets)) + " raw markets")

	filtered = []
	for m in markets:
		slug = m.get("slug", "")
		q = m.get("question", "")
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
		kind = "MATCH"
		low = (slug + " " + q).lower()
		if "o/u" in low or "total" in low or "over/under" in low:
			kind = "TOTAL"
		elif "spread" in low or "handicap" in low:
			kind = "SPREAD"
		elif "game" in low or "map" in low or "set" in low:
			kind = "MAP/SET"
		m["_kind"] = kind
		filtered.append(m)

	print("[*] " + str(len(filtered)) + " live candidates pass pre-filter")

	by_sport = {}
	for m in filtered:
		s = sport_of(m.get("slug", ""))
		by_sport[s] = by_sport.get(s, 0) + 1
	print("[*] breakdown:")
	for k in sorted(by_sport.keys()):
		print("      " + k.ljust(10) + " " + str(by_sport[k]))

	results = []
	reasons = {}
	for i, m in enumerate(filtered):
		r = analyze(m, min_notional)
		if isinstance(r, str):
			reasons[r] = reasons.get(r, 0) + 1
		else:
			r["sport"] = sport_of(r["slug"])
			r["tier"] = tier_from_league(m)
			r["market"] = m["_kind"]
			results.append(r)
		if (i + 1) % 20 == 0:
			print("    processed " + str(i + 1) + " / " + str(len(filtered)))
		time.sleep(0.5)

	print("[*] rejection reasons:")
	for k in sorted(reasons.keys()):
		print("      " + k.ljust(15) + " " + str(reasons[k]))

	if sport:
		results = [r for r in results if r["sport"].upper() == sport.upper()]
	if tier:
		results = [r for r in results if r["tier"].upper() == tier.upper()]

	results.sort(key=lambda x: x["over"])
	good = [r for r in results if r["over"] <= max_over]

	print()
	print("=" * 110)
	print("Top " + str(show) + " candidates (over<=" + str(max_over) + ", notional>=" + str(int(min_notional)) + ")")
	print("=" * 110)

	for i, r in enumerate(good[:show], 1):
		print()
		badge = "[" + r["sport"].ljust(8) + "/" + r["tier"].ljust(3) + "/" + r["market"].ljust(8) + "]"
		print("#" + str(i).ljust(3) + badge + "  over=" + format(r["over"], ".4f") + "  vol=" + format(r["vol"], ",.0f") + "  league=" + r["league"])
		print("     " + r["q"][:95])
		print("     start: " + r["start"] + "   end: " + r["end"])
		print("     slug: " + r["slug"])
		for oc, a, s, n in zip(r["o"], r["asks"], r["sizes"], r["notional"]):
			print("       " + oc[:28].ljust(28) + " ask=" + format(a, ".3f") + "  k=" + format(1.0 / a, ".2f") + "  liq=" + format(n, ",.0f"))

	print()
	print("[+] analyzed " + str(len(results)) + ", within threshold: " + str(len(good)))

	f = open("2way_candidates.txt", "w", encoding="utf-8")
	f.write("sport	tier	market	overround	volume24h	league	start	end	slug	question" + NL)
	for r in results:
		line = r["sport"] + "	" + r["tier"] + "	" + r["market"] + "	"
		line += format(r["over"], ".4f") + "	" + format(r["vol"], ".0f") + "	"
		line += r["league"] + "	" + r["start"] + "	" + r["end"] + "	"
		line += r["slug"] + "	" + r["q"].replace("	", " ")
		f.write(line + NL)
	f.close()
	print("[+] saved 2way_candidates.txt")

	slugs = [r["slug"] for r in good[:10]]
	b = open("run_bot.bat", "w", encoding="utf-8")
	b.write("@echo off" + NL)
	b.write("python polyarb_v3.py --slugs " + ",".join(slugs) + NL)
	b.write("pause" + NL)
	b.close()
	print("[+] wrote run_bot.bat with " + str(len(slugs)) + " slugs")


main()
