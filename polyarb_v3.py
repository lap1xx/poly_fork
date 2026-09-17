#!/usr/bin/env python3
# polyarb_v3.py v3 - live, colors, trades, csv, profiles
import argparse
import csv
import json
import os
import sqlite3
import sys
import threading
import time
import subprocess
import webbrowser
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
import requests

try:
	import websocket
	HAS_WS = True
except ImportError:
	HAS_WS = False

COLON = chr(58)
SLASH = chr(47)
DOT = chr(46)
HT = "https" + COLON + SLASH + SLASH
WSS = "wss" + COLON + SLASH + SLASH
GAMMA = HT + "gamma-api" + DOT + "polymarket" + DOT + "com"
WS_URL = WSS + "ws-subscriptions-clob" + DOT + "polymarket" + DOT + "com" + "/ws/market"
CBR_URL = HT + "www" + DOT + "cbr" + DOT + "ru" + "/scripts/XML_daily.asp"

TIMEOUT = 15.0
BOOKS = {}
LOCK = threading.Lock()
STOP = threading.Event()

class C:
	G = chr(27) + "[92m"
	R = chr(27) + "[91m"
	Y = chr(27) + "[93m"
	B = chr(27) + "[94m"
	M = chr(27) + "[95m"
	DIM = chr(27) + "[2m"
	RST = chr(27) + "[0m"

MELODY_PREMATCH = [(523, 180), (659, 180), (784, 180), (1047, 550)]
MELODY_LIVE = [(880, 120), (880, 120), (1047, 400)]

CFG_FILE = "config.json"
DB_FILE = "arbs.db"
CSV_FILE = "arbs.csv"
BK_FILE = "bk_profiles.json"

DEFAULT_CFG = {"bank": 20000, "rate": 0, "poll": 1.0, "slip": 0.0, "live_min_roi_show": -3.0}
DEFAULT_BK = {"PARI": {"comm": 0.0, "min_bet": 100}, "BetBoom": {"comm": 0.0, "min_bet": 50}, "Winline": {"comm": 0.0, "min_bet": 100}}
STREAMER_KEYS = ["streamer", "showmatch", "show-match", "influencer", "charity", "pro-am", "proam", "star-series", "star series", "esforce", "betboom", "winline", "parimatch", "youtube", "twitch"]

def load_cfg():
	if not os.path.exists(CFG_FILE):
		f = open(CFG_FILE, "w", encoding="utf-8")
		json.dump(DEFAULT_CFG, f, indent=2)
		f.close()
		return dict(DEFAULT_CFG)
	try:
		f = open(CFG_FILE, encoding="utf-8")
		c = json.load(f)
		f.close()
		o = dict(DEFAULT_CFG)
		o.update(c)
		return o
	except Exception:
		return dict(DEFAULT_CFG)

def load_bk():
	if not os.path.exists(BK_FILE):
		f = open(BK_FILE, "w", encoding="utf-8")
		json.dump(DEFAULT_BK, f, indent=2)
		f.close()
		return dict(DEFAULT_BK)
	try:
		f = open(BK_FILE, encoding="utf-8")
		d = json.load(f)
		f.close()
		return d
	except Exception:
		return dict(DEFAULT_BK)

def init_db():
	con = sqlite3.connect(DB_FILE)
	con.execute("CREATE TABLE IF NOT EXISTS arbs (ts TEXT, slug TEXT, sport TEXT, roi REAL, bank REAL, profit REAL, src1 TEXT, out1 TEXT, odds1 REAL, stake1 REAL, src2 TEXT, out2 TEXT, odds2 REAL, stake2 REAL)")
	con.execute("CREATE TABLE IF NOT EXISTS trades (ts TEXT, slug TEXT, source TEXT, bank_used REAL, profit REAL, note TEXT)")
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
		row = (ts, slug, sport, roi, bank, profit, b1.get("source", ""), b1.get("outcome", ""), b1.get("o", 0), s1, b2.get("source", ""), b2.get("outcome", ""), b2.get("o", 0), s2)
		con.execute("INSERT INTO arbs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", row)
		con.commit()
		con.close()
	except Exception:
		pass

def log_csv(slug, sport, roi, profit):
	try:
		newfile = not os.path.exists(CSV_FILE)
		f = open(CSV_FILE, "a", encoding="utf-8", newline="")
		w = csv.writer(f)
		if newfile:
			w.writerow(["ts", "slug", "sport", "roi", "profit"])
		w.writerow([datetime.now().strftime("%Y-%m-%d %H:%M:%S"), slug, sport, round(roi, 4), round(profit, 2)])
		f.close()
	except Exception:
		pass

def log_trade(slug, source, bank_used, profit, note):
	try:
		con = sqlite3.connect(DB_FILE)
		ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
		con.execute("INSERT INTO trades VALUES (?,?,?,?,?,?)", (ts, slug, source, bank_used, profit, note))
		con.commit()
		con.close()
		return True
	except Exception:
		return False

def today_stats():
	try:
		today = datetime.now().strftime("%Y-%m-%d")
		con = sqlite3.connect(DB_FILE)
		cur = con.execute("SELECT COUNT(*), COALESCE(SUM(profit),0) FROM arbs WHERE ts LIKE ?", (today + "%",))
		r = cur.fetchone()
		con.close()
		return int(r[0]), float(r[1])
	except Exception:
		return 0, 0.0

def beep(live=False):
	mel = MELODY_LIVE if live else MELODY_PREMATCH
	if sys.platform == "win32":
		try:
			import winsound
			for _ in range(2):
				for fr, d in mel:
					winsound.Beep(fr, d)
				time.sleep(0.15)
			return
		except Exception:
			pass
	for _ in range(8):
		sys.stdout.write(chr(7))
		sys.stdout.flush()
		time.sleep(0.25)

def fmt_ago(sec):
	if sec < 1:
		return "0s"
	m = int(sec // 60)
	s = int(sec - m * 60)
	if m > 0:
		return str(m) + "m " + str(s) + "s"
	return str(s) + "s"

def fmt_until(iso):
	if not iso:
		return ""
	try:
		s = iso.replace("Z", "+00:00").replace(" ", "T")
		dt = datetime.fromisoformat(s)
		now = datetime.now(timezone.utc)
		sec = (dt - now).total_seconds()
		if sec < 0:
			return "started"
		m = int(sec // 60)
		if m < 60:
			return "in " + str(m) + "m"
		h = int(m // 60)
		return "in " + str(h) + "h " + str(m - h * 60) + "m"
	except Exception:
		return ""

def is_streamer(slug, q):
	s = (slug + " " + q).lower()
	for k in STREAMER_KEYS:
		if k in s:
			return True
	return False

def is_live(slug):
	s = slug.lower()
	if "-live" in s or "inplay" in s or "-game" in s or "-set" in s:
		return True
	return False

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
						self._h(m)
					if time.time() - last > 50:
						try:
							ws.send("PING")
						except Exception:
							pass
						last = time.time()
			except Exception:
				time.sleep(2)

	def _h(self, raw):
		try:
			d = json.loads(raw)
		except Exception:
			return
		items = d if isinstance(d, list) else [d]
		for it in items:
			self._a(it)

	def _a(self, d):
		et = d.get("event_type")
		if et == "book":
			aid = d.get("asset_id")
			if not aid:
				return
			bids = []
			asks = []
			for x in d.get("bids", []) or []:
				try:
					sz = float(x["size"])
					if sz > 0:
						bids.append((float(x["price"]), sz))
				except Exception:
					pass
			for x in d.get("asks", []) or []:
				try:
					sz = float(x["size"])
					if sz > 0:
						asks.append((float(x["price"]), sz))
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

def walk_book(aid, target):
	with LOCK:
		b = BOOKS.get(aid)
		if not b:
			return None
		levels = list(b["asks"])
	rem = target
	total = 0.0
	for p, s in levels:
		t = min(rem, s)
		total += t * p
		rem -= t
		if rem <= 1e-9:
			break
	if rem > 1e-9:
		return None
	return total / target

def get_rate():
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
		return None
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

def refine_stakes(bets, result, rate):
	if not result:
		return None
	for _ in range(3):
		changed = False
		for i, b in enumerate(bets):
			if b.get("source") != "Polymarket":
				continue
			stake_rub = result["stakes"][i]
			if stake_rub <= 0:
				continue
			best_p = best_ask(b.get("token", ""))
			if best_p <= 0:
				continue
			shares = stake_rub / (best_p * rate)
			avg = walk_book(b.get("token", ""), shares)
			if not avg or avg <= 0:
				continue
			new_o = 1.0 / (avg * rate)
			if abs(new_o - b["o"]) / b["o"] > 0.0005:
				b["o"] = new_o
				changed = True
		if not changed:
			break
		result = compute(bets, result["payout"] / (1 + result["roi"]))
	return result

def classify(slug):
	s = slug.lower()
	tbl = [
		("cs2-", "CS2"), ("dota2-", "DOTA2"), ("lol-", "LOL"), ("valorant-", "VALORANT"),
		("r6-", "R6"), ("rl-", "ROCKET"), ("cod-", "COD"),
		("atp-", "TENNIS"), ("wta-", "TENNIS"), ("itf-", "TENNIS"),
	]
	for p, n in tbl:
		if s.startswith(p):
			return n
	return "OTHER"

def sport_color(sport):
	if sport == "CS2":
		return C.G
	if sport == "DOTA2":
		return C.M
	if sport == "TENNIS":
		return C.B
	return C.RST


def copy_to_clipboard(text):
	if sys.platform == "win32":
		try:
			p = subprocess.Popen(["clip"], stdin=subprocess.PIPE, shell=True)
			p.communicate(input=text.encode("utf-16-le"))
			return True
		except Exception:
			return False
	return False

def main():
	ap = argparse.ArgumentParser(prog="polyarb_v3")
	ap.add_argument("--slugs", required=True)
	ap.add_argument("--bank", type=float, default=0.0)
	ap.add_argument("--rate", type=float, default=0.0)
	ap.add_argument("--slip", type=float, default=-1.0)
	ap.add_argument("--poll", type=float, default=0.0)
	ap.add_argument("--live", action="store_true")
	ap.add_argument("--min-roi-show", type=float, default=None)
	args = ap.parse_args()

	if not HAS_WS:
		print("[!] pip install websocket-client")
		return 1

	cfg = load_cfg()
	bk = load_bk()
	init_db()
	poll = args.poll if args.poll > 0 else (0.1 if args.live else cfg.get("poll", 1.0))
	bank = args.bank if args.bank > 0 else cfg.get("bank", 20000)
	slip = args.slip if args.slip >= 0 else cfg.get("slip", 0.0)
	min_roi_show = args.min_roi_show if args.min_roi_show is not None else cfg.get("live_min_roi_show", -3.0)
	rate = args.rate if args.rate > 0 else cfg.get("rate", 0)
	if rate <= 0:
		rate = get_rate()
	if not rate:
		print("[!] cannot fetch USD/RUB, pass --rate")
		return 1
	mode_tag = C.Y + "LIVE" + C.RST if args.live else C.B + "PREMATCH" + C.RST
	print("=" * 70)
	print("  POLY-ARB v3  [" + mode_tag + "]  USD/RUB=" + format(rate, ".2f") + "  bank=" + format(bank, ",.0f"))
	print("=" * 70)

	slug_list = [s.strip() for s in args.slugs.split(",") if s.strip()]
	all_markets = []
	all_tokens = []
	for slug in slug_list:
		try:
			r = requests.get(GAMMA + "/markets", params={"slug": slug}, timeout=TIMEOUT)
			r.raise_for_status()
			d = r.json()
		except Exception as e:
			print("[!] fetch error " + slug + ": " + str(e))
			continue
		if not d:
			print("[!] skip " + slug)
			continue
		m = d[0]
		rec = {"slug": m["slug"], "q": m.get("question", ""), "o": json.loads(m["outcomes"]), "t": json.loads(m["clobTokenIds"]), "start": m.get("eventStartTime", "") or m.get("gameStartTime", "")}
		all_markets.append(rec)
		all_tokens.extend(rec["t"])
		sport = classify(rec["slug"])
		sc = sport_color(sport)
		streamer_tag = C.Y + " [STREAMER]" + C.RST if is_streamer(rec["slug"], rec["q"]) else ""
		print("[+] " + sc + "[" + sport + "]" + C.RST + " " + rec["q"] + streamer_tag)

	if not all_markets:
		return 1

	feed = Feed(all_tokens)
	feed.start()
	print("[*] waiting for WS book...")
	ready = False
	for _ in range(40):
		with LOCK:
			ready = all(t in BOOKS for t in all_tokens)
		if ready:
			break
		time.sleep(0.5)
	if not ready:
		print(C.R + "[!] WS book not received" + C.RST)
		STOP.set()
		return 1
	print(C.G + "[+] WS ready" + C.RST)

	bc_data = {}
	for m in all_markets:
		print()
		print(C.B + "--- " + m["q"] + " ---" + C.RST)
		for oc, tid in zip(m["o"], m["t"]):
			a = best_ask(tid)
			kk = (1.0 / a) if a > 0 else 0.0
			print("  PM " + oc.ljust(24) + " ask=" + format(a, ".3f") + "  k=" + format(kk, ".2f"))
		crm = {}
		for oc in m["o"]:
			raw = input("  BC odds for " + oc + " (ex: BetBoom 1.85, Enter=skip): ").strip()
			if not raw:
				continue
			p = raw.split()
			src = p[0] if len(p) > 1 else "BC"
			try:
				od = float(p[-1].replace(",", "."))
			except ValueError:
				continue
			comm = bk.get(src, {}).get("comm", 0.0)
			crm[oc] = {"src": src, "o": od, "c": comm}
		bc_data[m["slug"]] = crm

	print()
	print(C.DIM + "[*] Ctrl+C to exit. [T] after arb to log trade." + C.RST)
	print()

	first_seen = {}
	session_count = 0
	session_profit = 0.0

	try:
		while True:
			best_roi = -999
			best_m = None
			best_bets = None
			best_r = None
			for m in all_markets:
				bets = []
				for oc, tid in zip(m["o"], m["t"]):
					a = best_ask(tid)
					po = (1.0 / (a * rate)) if a > 0 else 0.0
					pm = eff_mult(po, 0.0, slip)
					bco = bc_data.get(m["slug"], {}).get(oc, {})
					bo = bco.get("o", 0.0)
					bm = eff_mult(bo, 0.0, 0.0)
					if bm > 0 and bm >= pm:
						bets.append({"outcome": oc, "source": bco["src"], "o": bo, "c": bco.get("c", 0), "s": 0.0})
					elif po > 0:
						bets.append({"outcome": oc, "source": "Polymarket", "o": po, "c": 0.0, "s": slip, "token": tid})
				if len(bets) != len(m["o"]):
					continue
				r = compute(bets, bank)
				r = refine_stakes(bets, r, rate) if r else None
				if not r:
					continue
				if r["roi"] > best_roi:
					best_roi = r["roi"]
					best_m = m
					best_bets = bets
					best_r = r

			ts = time.strftime("%H:%M:%S")
			if best_roi >= min_roi_show or best_roi > 0:
				roi_col = C.G if best_roi > 0 else (C.Y if best_roi > -1 else C.R)
				sport = classify(best_m["slug"]) if best_m else "?"
				sc = sport_color(sport)
				slug_short = best_m["slug"][:28].ljust(28) if best_m else "no data"
				print("[" + ts + "] " + sc + slug_short + C.RST + " ROI=" + roi_col + format(best_roi * 100, "+.3f") + "%" + C.RST + " session: " + str(session_count) + " / +" + format(session_profit, ",.0f") + " rub")

			if best_roi > 0 and best_m and best_m["slug"] not in first_seen:
				first_seen[best_m["slug"]] = time.time()
				is_live_m = is_live(best_m["slug"])
				beep(is_live_m)
				sport = classify(best_m["slug"])
				sc = sport_color(sport)
				profit_b = best_r["payout"] - bank
				log_arb(best_m["slug"], sport, best_r["roi"], bank, profit_b, best_bets, best_r["stakes"])
				log_csv(best_m["slug"], sport, best_r["roi"], profit_b)
				session_count += 1
				session_profit += profit_b
				print()
				print(C.G + "=" * 70 + C.RST)
				live_tag = C.Y + " [LIVE]" + C.RST if is_live_m else ""
				stream_tag = C.Y + " [STREAMER]" + C.RST if is_streamer(best_m["slug"], best_m["q"]) else ""
				print(C.G + "[+] ARB" + C.RST + " " + sc + "[" + sport + "]" + C.RST + live_tag + stream_tag + " " + best_m["q"])
				print(C.G + "=" * 70 + C.RST)
				for i in range(len(best_bets)):
					b = best_bets[i]
					st = best_r["stakes"][i]
					ln = "  " + b["source"].ljust(12) + " " + b["outcome"].ljust(24)
					ln += " k=" + format(b["o"], ".3f") + "  stake=" + format(st, ",.0f") + " rub"
					print(C.G + ln + C.RST)
				until = fmt_until(best_m.get("start", ""))
				print()
				print("  ROI:      " + format(best_r["roi"] * 100, "+.3f") + "%")
				print("  Profit:   " + format(profit_b, ",.0f") + " rub")
				print("  Type:     " + ("LIVE" if is_live_m else "PREMATCH"))
				if until:
					print("  Start:    " + until)
				print("  Age:      " + fmt_ago(time.time() - first_seen[best_m["slug"]]))
				print("  Session:  " + str(session_count) + " arbs, +" + format(session_profit, ",.0f") + " rub")
				print(C.G + "=" * 70 + C.RST)
				print()
				print(C.Y + "[*] Place bets NOW. [Enter] resume / [T] log trade" + C.RST)
				try:
					cmd = input().strip().lower()
				except EOFError:
					cmd = ""
				if cmd == "t":
					bank_used = best_r["payout"] / (1 + best_r["roi"]) if best_r["roi"] > -1 else 0
					ok = log_trade(best_m["slug"], "manual", bank_used, profit_b, "")
					print(C.G + "[>] logged trade: " + str(ok) + C.RST)
				print(C.DIM + "[*] Resuming..." + C.RST)
				time.sleep(0.5)
			else:
				keep = set([best_m["slug"]]) if best_m else set()
				for s in list(first_seen.keys()):
					if s not in keep:
						del first_seen[s]

			time.sleep(poll)
	except KeyboardInterrupt:
		print()
		print(C.Y + "Bye. Session: " + str(session_count) + " arbs, +" + format(session_profit, ",.0f") + " rub" + C.RST)
		STOP.set()
		return 0

sys.exit(main())
