#!/usr/bin/env python3
# polyarb_v3.py - config-aware arb scanner with SQLite logging.
import argparse
import json
import os
import sqlite3
import sys
import threading
import time
import xml.etree.ElementTree as ET
from datetime import datetime
import requests

try:
	import websocket
	HAS_WS = True
except ImportError:
	HAS_WS = False

COLON = chr(58)
SLASH = chr(47)
DOT = chr(46)
HT = "http" + chr(115) + COLON + SLASH + SLASH
WSS = "wss" + COLON + SLASH + SLASH
GAMMA = HT + "gamma-api" + DOT + "polymarket" + DOT + "com"
WS_URL = WSS + "ws-subscriptions-clob" + DOT + "polymarket" + DOT + "com" + "/ws/market"
CBR_URL = HT + "www" + DOT + "cbr" + DOT + "ru" + "/scripts/XML_daily.asp"

TIMEOUT = 15.0
BOOKS = {}
LOCK = threading.Lock()
STOP = threading.Event()
MELODY = [(523, 220), (659, 220), (784, 220), (1047, 650)]
CFG_FILE = "config.json"
DB_FILE = "arbs.db"
DEFAULT_CFG = {
	"bank": 20000,
	"rate": 0,
	"scan": {"top": 800, "show": 40, "min_volume": 500, "min_notional": 500, "max_overround": 1.05, "sport": "", "tier": ""},
	"bot": {"poll": 1.0, "slip": 0.0},
}


def load_cfg():
	if os.path.exists(CFG_FILE):
		try:
			f = open(CFG_FILE, encoding="utf-8")
			c = json.load(f)
			f.close()
			out = dict(DEFAULT_CFG)
			out.update(c)
			return out
		except Exception:
			pass
	return dict(DEFAULT_CFG)


def init_db():
	con = sqlite3.connect(DB_FILE)
	sql = "CREATE TABLE IF NOT EXISTS arbs ("
	sql += "ts TEXT, slug TEXT, sport TEXT, roi REAL, bank REAL, profit REAL, "
	sql += "src1 TEXT, out1 TEXT, odds1 REAL, stake1 REAL, "
	sql += "src2 TEXT, out2 TEXT, odds2 REAL, stake2 REAL)"
	con.execute(sql)
	con.commit()
	con.close()


def log_arb(slug, sport, roi, bank, profit, bets, stakes):
	try:
		con = sqlite3.connect(DB_FILE)
		b1 = bets[0] if len(bets) > 0 else {}
		b2 = bets[1] if len(bets) > 1 else {}
		s1 = stakes[0] if len(stakes) > 0 else 0.0
		s2 = stakes[1] if len(stakes) > 1 else 0.0
		ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
		row = (ts, slug, sport, roi, bank, profit,
			b1.get("source", ""), b1.get("outcome", ""), b1.get("o", 0), s1,
			b2.get("source", ""), b2.get("outcome", ""), b2.get("o", 0), s2)
		con.execute("INSERT INTO arbs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", row)
		con.commit()
		con.close()
	except Exception:
		pass


def today_stats():
	try:
		today = datetime.now().strftime("%Y-%m-%d")
		con = sqlite3.connect(DB_FILE)
		cur = con.execute("SELECT COUNT(*), COALESCE(SUM(profit),0) FROM arbs WHERE ts LIKE ?", (today + "%",))
		row = cur.fetchone()
		con.close()
		return int(row[0]), float(row[1])
	except Exception:
		return 0, 0.0


def beep_melody():
	if sys.platform == "win32":
		try:
			import winsound
			for _ in range(2):
				for f, d in MELODY:
					winsound.Beep(f, d)
				time.sleep(0.18)
			return
		except Exception:
			pass
	for _ in range(6):
		sys.stdout.write(chr(7))
		sys.stdout.flush()
		time.sleep(0.25)


class Feed(threading.Thread):
	def __init__(self, ids):
		super().__init__(daemon=True)
		self.ids = ids

	def run(self):
		while not STOP.is_set():
			try:
				ws = websocket.create_connection(WS_URL, timeout=10)
				p = {"type": "market", "assets_ids": self.ids, "initial_dump": True}
				ws.send(json.dumps(p))
				last = time.time()
				while not STOP.is_set():
					ws.settimeout(1.0)
					try:
						m = ws.recv()
					except Exception:
						m = None
					if m and m != "PONG":
						self._handle(m)
					if time.time() - last > 50:
						try:
							ws.send("PING")
						except Exception:
							pass
						last = time.time()
			except Exception as e:
				print("[!] WS: " + str(e))
				time.sleep(2)

	def _handle(self, raw):
		try:
			d = json.loads(raw)
		except Exception:
			return
		items = d if isinstance(d, list) else [d]
		for it in items:
			self._apply(it)

	def _apply(self, d):
		et = d.get("event_type")
		if et == "book":
			aid = d.get("asset_id")
			if not aid:
				return
			bids = []
			asks = []
			for x in d.get("bids", []) or []:
				try:
					s = float(x["size"])
					if s > 0:
						bids.append((float(x["price"]), s))
				except Exception:
					pass
			for x in d.get("asks", []) or []:
				try:
					s = float(x["size"])
					if s > 0:
						asks.append((float(x["price"]), s))
				except Exception:
					pass
			bids.sort(key=lambda z: -z[0])
			asks.sort(key=lambda z: z[0])
			with LOCK:
				BOOKS[aid] = {"bids": bids, "asks": asks}


def best_ask(aid):
	with LOCK:
		b = BOOKS.get(aid)
		if not b or not b["asks"]:
			return 0.0
		return b["asks"][0][0]


def fetch_market(slug):
	r = requests.get(GAMMA + "/markets", params={"slug": slug}, timeout=TIMEOUT)
	r.raise_for_status()
	d = r.json()
	if not d:
		return None
	m = d[0]
	return {"slug": m["slug"], "q": m.get("question", ""),
		"o": json.loads(m["outcomes"]),
		"t": json.loads(m["clobTokenIds"])}


def get_usd_rate():
	try:
		r = requests.get(CBR_URL, timeout=10)
		r.raise_for_status()
		root = ET.fromstring(r.content)
		for v in root.findall("Valute"):
			c = v.find("CharCode")
			if c is not None and c.text == "USD":
				val = v.find("Value")
				nom = v.find("Nominal")
				if val is not None and nom is not None:
					return float(val.text.replace(",", ".")) / float(nom.text)
	except Exception:
		pass
	return None


def eff_mult(odds, comm, slip):
	if odds <= 1.0:
		return 0.0
	x = odds * (1.0 - slip)
	if x <= 1.0:
		return 0.0
	return 1.0 + (x - 1.0) * (1.0 - comm)


def compute(bets, bank):
	ms = [eff_mult(b["o"], b["c"], b["s"]) for b in bets]
	if any(m <= 0 for m in ms):
		return None
	inv = sum(1.0 / m for m in ms)
	roi = 1.0 / inv - 1.0
	stakes = [bank * (1.0 / m) / inv for m in ms]
	return {"roi": roi, "stakes": stakes, "payout": bank / inv}


def classify_sport(slug):
	s = slug.lower()
	tbl = [
		("cs2-", "CS2"), ("dota2-", "DOTA2"), ("lol-", "LOL"), ("valorant-", "VALORANT"),
		("r6-", "R6"), ("rl-", "ROCKET"), ("cod-", "COD"), ("pubg-", "PUBG"),
		("atp-", "TENNIS"), ("wta-", "TENNIS"), ("itf-", "TENNIS"),
		("nba-", "NBA"), ("euroleague-", "EURO"), ("nfl-", "NFL"),
		("nhl-", "NHL"), ("khl-", "KHL"), ("mlb-", "MLB"),
		("ufc-", "MMA"), ("boxing-", "BOXING"),
	]
	for p, name in tbl:
		if s.startswith(p):
			return name
	return "OTHER"


def fmt_age(sec):
	if sec < 1:
		return "0s"
	m = int(sec // 60)
	s = int(sec - m * 60)
	if m > 0:
		return str(m) + "m " + str(s) + "s"
	return str(s) + "s"


def detect_type(slug):
	s = slug.lower()
	if "-live" in s or "inplay" in s:
		return "LIVE"
	return "PREMATCH"


def detect_streamer(slug, question):
	s = (slug + " " + question).lower()
	keys = ["streamer", "showmatch", "show-match", "influencer", "charity", "pro-am", "proam", "star-series", "star series", "esforce"]
	for k in keys:
		if k in s:
			return True
	return False


def main():
	ap = argparse.ArgumentParser(prog="polyarb_v3")
	ap.add_argument("--slugs", required=True)
	ap.add_argument("--bank", type=float, default=0.0)
	ap.add_argument("--rate", type=float, default=0.0)
	ap.add_argument("--slip", type=float, default=-1.0)
	ap.add_argument("--poll", type=float, default=0.0)
	args = ap.parse_args()

	if not HAS_WS:
		print("[!] pip install websocket-client")
		return 1

	cfg = load_cfg()
	init_db()
	bot_cfg = cfg.get("bot", {})
	bank = args.bank if args.bank > 0 else cfg.get("bank", 20000)
	poll = args.poll if args.poll > 0 else bot_cfg.get("poll", 1.0)
	slip = args.slip if args.slip >= 0 else bot_cfg.get("slip", 0.0)
	rate = args.rate if args.rate > 0 else cfg.get("rate", 0)
	if rate <= 0:
		rate = get_usd_rate()
	if not rate:
		print("[!] cannot fetch USD/RUB, pass --rate")
		return 1

	print("=" * 70)
	print("  POLY-ARB v3 - logging enabled")
	print("=" * 70)
	print("[+] USD/RUB = " + format(rate, ".2f") + "  Bank: " + format(bank, ",.0f") + " rub  poll=" + str(poll))
	print("[+] DB: " + DB_FILE)
	print()

	slugs = [s.strip() for s in args.slugs.split(",") if s.strip()]
	markets = []
	all_tokens = []
	for s in slugs:
		m = fetch_market(s)
		if not m:
			print("[!] skip " + s)
			continue
		markets.append(m)
		all_tokens.extend(m["t"])
		print("[+] " + m["q"])

	if not markets:
		return 1

	feed = Feed(all_tokens)
	feed.start()
	print()
	print("[*] waiting for WS book...")
	ready = False
	for _ in range(40):
		with LOCK:
			ready = all(t in BOOKS for t in all_tokens)
		if ready:
			break
		time.sleep(0.5)
	if not ready:
		print("[!] WS book not received")
		STOP.set()
		return 1
	print("[+] WS ready")

	bc_data = {}
	for m in markets:
		print()
		print("--- " + m["q"] + " ---")
		for oc, tid in zip(m["o"], m["t"]):
			a = best_ask(tid)
			kk = (1.0 / a) if a > 0 else 0.0
			print("  PM " + oc.ljust(24) + " ask=" + format(a, ".3f") + "  k=" + format(kk, ".2f"))
		crm = {}
		for oc in m["o"]:
			raw = input("  BC odds for " + oc + " (Enter=skip): ").strip()
			if not raw:
				continue
			p = raw.split()
			src = p[0] if len(p) > 1 else "BC"
			try:
				od = float(p[-1].replace(",", "."))
			except ValueError:
				continue
			crm[oc] = {"src": src, "o": od, "c": 0.0}
		bc_data[m["slug"]] = crm

	print()
	print("[*] monitoring " + str(len(markets)) + " markets. Ctrl+C to exit.")
	print()

	first_seen = {}
	try:
		while True:
			for m in markets:
				bets = []
				for oc, tid in zip(m["o"], m["t"]):
					a = best_ask(tid)
					po = (1.0 / (a * rate)) if a > 0 else 0.0
					pm = eff_mult(po, 0.0, slip)
					bco = bc_data[m["slug"]].get(oc, {})
					bo = bco.get("o", 0.0)
					bm = eff_mult(bo, 0.0, 0.0)
					if bm > 0 and bm >= pm:
						bets.append({"outcome": oc, "source": bco["src"], "o": bo, "c": 0.0, "s": 0.0})
					elif po > 0:
						bets.append({"outcome": oc, "source": "Polymarket", "o": po, "c": 0.0, "s": slip, "token": tid})
				if len(bets) != len(m["o"]):
					continue
				rr = compute(bets, bank)
				if not rr:
					continue
				roi = rr["roi"]
				sport = classify_sport(m["slug"])
				ts = time.strftime("%H:%M:%S")
				n_today, p_today = today_stats()
				if roi > 0:
					slug = m["slug"]
					if slug not in first_seen:
						first_seen[slug] = time.time()
						beep_melody()
						profit_b = rr["payout"] - bank
						log_arb(slug, sport, roi, bank, profit_b, bets, rr["stakes"])
					print()
					print("=" * 70)
					print("[+] ARB [" + sport + "]: " + m["q"])
					print("=" * 70)
					for i in range(len(bets)):
						b = bets[i]
						st = rr["stakes"][i]
						ln = "  " + b["source"].ljust(12) + " " + b["outcome"].ljust(24)
						ln += " k=" + format(b["o"], ".3f") + "  stake=" + format(st, ",.0f") + " rub"
						print(ln)
					print()
					print("  ROI:     " + format(roi * 100, "+.3f") + "%")
					print("  Payout:  " + format(rr["payout"], ",.0f") + " rub")
					print("  Profit:  " + format(rr["payout"] - bank, ",.0f") + " rub")
					print("  Age:     " + str(int(time.time() - first_seen[slug])) + "s")
					print("  Today:   " + str(n_today) + " arbs, +" + format(p_today, ",.0f") + " rub")
					print("=" * 70)
					print()
					print("[*] Place bets NOW. Press Enter to resume (Ctrl+C to exit).")
					try:
						input()
					except EOFError:
						pass
					print("[*] Resuming...")
					time.sleep(0.5)
					continue
				top = m["slug"][:26].ljust(26)
				line = "[" + ts + "] " + top + " ROI=" + format(roi * 100, "+.3f") + "%  [" + sport + "]"
				line += "  today: " + str(n_today) + "/+" + format(p_today, ",.0f")
				print(line)
			time.sleep(poll)
	except KeyboardInterrupt:
		print()
		print("Bye")
		STOP.set()
		return 0


sys.exit(main())
