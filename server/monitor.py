#!/usr/bin/env python3
"""Live Polymarket CLOB monitor for Mini App desk."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
import xml.etree.ElementTree as ET
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Any

import requests
from arb_invariants import is_pm_bookmaker_pair

try:
	import websocket
	HAS_WS = True
except ImportError:
	HAS_WS = False

ROOT = Path(__file__).resolve().parents[1]
CFG_FILE = ROOT / "config.json"
DB_FILE = ROOT / "arbs.db"
BC_FILE = ROOT / "bc_odds.json"
WATCH_FILE = ROOT / "watch_slugs.json"

GAMMA = "https://gamma-api.polymarket.com"
WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
CBR_URL = "https://www.cbr.ru/scripts/XML_daily.asp"
TIMEOUT = 15.0

DEFAULT_SLUGS = [
	"wta-linana-llorca-2026-09-14",
	"cs2-forzer-upgrad-2026-09-15",
	"wta-carle-gorgodz-2026-09-15",
	"cs2-baks-tsa-2026-09-15",
	"wta-selekhm-schunk-2026-09-15",
	"wta-ruggeri-vedder-2026-09-14",
	"wta-stefani-podoros-2026-09-14",
	"cs2-eace-phanto-2026-09-15",
	"wta-maneiro-salkova-2026-09-14",
	"wta-brace-sierra-2026-09-14",
]


def classify_sport(slug: str) -> str:
	s = slug.lower()
	tbl = [
		("cs2-", "CS2"), ("dota2-", "DOTA2"), ("lol-", "LOL"), ("valorant-", "VALORANT"),
		("atp-", "TENNIS"), ("wta-", "TENNIS"), ("itf-", "TENNIS"),
	]
	for p, name in tbl:
		if s.startswith(p):
			return name
	return "OTHER"


def eff_mult(odds: float, comm: float = 0.0, slip: float = 0.0) -> float:
	if odds <= 1.0:
		return 0.0
	x = odds * (1.0 - slip)
	if x <= 1.0:
		return 0.0
	return 1.0 + (x - 1.0) * (1.0 - comm)


def compute(bets: list[dict], bank: float) -> dict | None:
	ms = [eff_mult(b["o"], b.get("c", 0.0), b.get("s", 0.0)) for b in bets]
	if any(m <= 0 for m in ms):
		return None
	inv = sum(1.0 / m for m in ms)
	roi = 1.0 / inv - 1.0
	stakes = [bank * (1.0 / m) / inv for m in ms]
	return {"roi": roi, "stakes": stakes, "payout": bank / inv}


def get_usd_rate() -> float | None:
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


def load_cfg() -> dict[str, Any]:
	if CFG_FILE.exists():
		try:
			return json.loads(CFG_FILE.read_text(encoding="utf-8"))
		except Exception:
			pass
	return {"bank": 20000, "rate": 0, "bot": {"poll": 1.0, "slip": 0.0}}


def load_watch_slugs() -> list[str]:
	if WATCH_FILE.exists():
		try:
			data = json.loads(WATCH_FILE.read_text(encoding="utf-8"))
			if isinstance(data, list) and data:
				return [str(x) for x in data]
		except Exception:
			pass
	return list(DEFAULT_SLUGS)


def save_watch_slugs(slugs: list[str]) -> None:
	WATCH_FILE.write_text(json.dumps(slugs, indent=2), encoding="utf-8")


def load_bc_odds() -> dict[str, dict[str, dict[str, Any]]]:
	"""slug -> outcome -> {src, o}"""
	out: dict[str, dict[str, dict[str, Any]]] = {}
	if not BC_FILE.exists():
		return out
	try:
		rows = json.loads(BC_FILE.read_text(encoding="utf-8"))
	except Exception:
		return out
	for row in rows:
		slug = row.get("slug") or ""
		oc = row.get("outcome") or ""
		try:
			od = float(str(row.get("odds", "")).replace(",", "."))
		except Exception:
			continue
		if not slug or not oc or od <= 1.0:
			continue
		out.setdefault(slug, {})[oc] = {
			"src": row.get("source") or "BC",
			"o": od,
		}
	return out


def init_db() -> None:
	con = sqlite3.connect(DB_FILE)
	con.execute(
		"CREATE TABLE IF NOT EXISTS arbs ("
		"ts TEXT, slug TEXT, sport TEXT, roi REAL, bank REAL, profit REAL, "
		"src1 TEXT, out1 TEXT, odds1 REAL, stake1 REAL, "
		"src2 TEXT, out2 TEXT, odds2 REAL, stake2 REAL)"
	)
	con.commit()
	con.close()


def log_arb(slug: str, sport: str, roi: float, bank: float, profit: float, bets: list, stakes: list) -> None:
	try:
		con = sqlite3.connect(DB_FILE)
		b1 = bets[0] if bets else {}
		b2 = bets[1] if len(bets) > 1 else {}
		s1 = stakes[0] if stakes else 0.0
		s2 = stakes[1] if len(stakes) > 1 else 0.0
		ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
		con.execute(
			"INSERT INTO arbs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
			(
				ts, slug, sport, roi, bank, profit,
				b1.get("source", ""), b1.get("outcome", ""), b1.get("o", 0), s1,
				b2.get("source", ""), b2.get("outcome", ""), b2.get("o", 0), s2,
			),
		)
		con.commit()
		con.close()
	except Exception:
		pass


def today_stats() -> tuple[int, float]:
	try:
		today = datetime.now().strftime("%Y-%m-%d")
		con = sqlite3.connect(DB_FILE)
		cur = con.execute(
			"SELECT COUNT(*), COALESCE(SUM(profit),0) FROM arbs WHERE ts LIKE ?",
			(today + "%",),
		)
		row = cur.fetchone()
		con.close()
		return int(row[0]), float(row[1])
	except Exception:
		return 0, 0.0


class Feed(threading.Thread):
	def __init__(self, ids: list[str], books: dict, lock: threading.Lock, stop: threading.Event, feed_log):
		super().__init__(daemon=True)
		self.ids = ids
		self.books = books
		self.lock = lock
		self.stop = stop
		self.feed_log = feed_log
		self.ready = False

	def run(self) -> None:
		if not HAS_WS or not self.ids:
			self.feed_log("warn", "websocket-client missing or no tokens")
			return
		while not self.stop.is_set():
			try:
				ws = websocket.create_connection(WS_URL, timeout=10)
				ws.send(json.dumps({"type": "market", "assets_ids": self.ids, "initial_dump": True}))
				self.feed_log("accent", "WS connected - %d tokens" % len(self.ids))
				self.ready = True
				last = time.time()
				while not self.stop.is_set():
					ws.settimeout(1.0)
					try:
						raw = ws.recv()
					except Exception:
						raw = None
					if raw and raw != "PONG":
						self._handle(raw)
					if time.time() - last > 50:
						try:
							ws.send("PING")
						except Exception:
							pass
						last = time.time()
			except Exception as e:
				self.ready = False
				self.feed_log("warn", "WS: %s" % e)
				time.sleep(2)

	def _handle(self, raw: str) -> None:
		try:
			d = json.loads(raw)
		except Exception:
			return
		items = d if isinstance(d, list) else [d]
		for it in items:
			self._apply(it)

	def _apply(self, d: dict) -> None:
		if d.get("event_type") != "book":
			return
		aid = d.get("asset_id")
		if not aid:
			return
		asks = []
		for x in d.get("asks") or []:
			try:
				s = float(x["size"])
				if s > 0:
					asks.append((float(x["price"]), s))
			except Exception:
				pass
		asks.sort(key=lambda z: z[0])
		with self.lock:
			cur = self.books.get(aid) or {}
			cur["asks"] = asks
			self.books[aid] = cur


class Monitor:
	def __init__(self) -> None:
		self.lock = threading.Lock()
		self.stop = threading.Event()
		self.books: dict[str, dict] = {}
		self.markets: list[dict] = []
		self.feed: Feed | None = None
		self.loop_thread: threading.Thread | None = None
		self.started_at = time.time()
		self.feed_items: deque[dict] = deque(maxlen=40)
		self.first_seen: dict[str, float] = {}
		self.rate = 84.0
		self.bank = 20000.0
		self.slip = 0.0
		self.poll = 1.0
		self._desk_cache: dict[str, Any] = {}
		self._desk_lock = threading.Lock()

	def log(self, tone: str, text: str) -> None:
		item = {"ts": time.strftime("%H:%M:%S"), "text": text, "tone": tone}
		self.feed_items.appendleft(item)

	def start(self) -> None:
		init_db()
		cfg = load_cfg()
		self.bank = float(cfg.get("bank") or 20000)
		self.slip = float((cfg.get("bot") or {}).get("slip") or 0)
		self.poll = float((cfg.get("bot") or {}).get("poll") or 1)
		rate = float(cfg.get("rate") or 0)
		if rate <= 0:
			rate = get_usd_rate() or 84.0
		self.rate = rate
		self.log("accent", "USD/RUB=%.2f bank=%.0f" % (self.rate, self.bank))

		slugs = load_watch_slugs()
		markets = []
		tokens: list[str] = []
		for slug in slugs:
			m = self.fetch_market(slug)
			if not m:
				self.log("warn", "skip %s" % slug)
				continue
			markets.append(m)
			tokens.extend(m["t"])
			self.log("mute", "load %s" % slug[:28])
		self.markets = markets

		if tokens and HAS_WS:
			self.feed = Feed(tokens, self.books, self.lock, self.stop, self.log)
			self.feed.start()
			# wait briefly for books
			for _ in range(30):
				with self.lock:
					ready = all(t in self.books for t in tokens)
				if ready:
					break
				time.sleep(0.3)
			self.log("accent", "WS ready" if self.feed.ready else "WS warming…")
		else:
			self.log("warn", "no WS feed")

		self.loop_thread = threading.Thread(target=self._loop, daemon=True)
		self.loop_thread.start()
		self.log("accent", "monitor loop on - %d markets" % len(self.markets))

	def stop_all(self) -> None:
		self.stop.set()

	def fetch_market(self, slug: str) -> dict | None:
		try:
			r = requests.get(GAMMA + "/markets", params={"slug": slug}, timeout=TIMEOUT)
			r.raise_for_status()
			d = r.json()
			if not d:
				return None
			m = d[0]
			return {
				"slug": m["slug"],
				"q": m.get("question", ""),
				"o": json.loads(m["outcomes"]),
				"t": json.loads(m["clobTokenIds"]),
			}
		except Exception as e:
			self.log("warn", "gamma %s: %s" % (slug[:20], e))
			return None

	def best_ask(self, tid: str) -> float:
		with self.lock:
			b = self.books.get(tid)
			if not b or not b.get("asks"):
				return 0.0
			return float(b["asks"][0][0])

	def _loop(self) -> None:
		while not self.stop.is_set():
			try:
				self._tick()
			except Exception as e:
				self.log("warn", "tick: %s" % e)
			time.sleep(max(0.5, self.poll))

	def _tick(self) -> None:
		cfg = load_cfg()
		self.bank = float(cfg.get("bank") or self.bank)
		self.slip = float((cfg.get("bot") or {}).get("slip") or 0)
		bc = load_bc_odds()
		n_today, p_today = today_stats()
		watchlist = []
		active = None
		best_roi = -999.0

		for m in self.markets:
			asks = [self.best_ask(tid) for tid in m["t"]]
			# Correct PM decimal odds = 1/ask (currency-independent)
			pm_odds = [(1.0 / a) if a > 0 else 0.0 for a in asks]
			bets = []
			slug_bc = bc.get(m["slug"], {})

			for i, oc in enumerate(m["o"]):
				po = pm_odds[i] if i < len(pm_odds) else 0.0
				pm = eff_mult(po, 0.0, self.slip)
				bco = slug_bc.get(oc, {})
				bo = float(bco.get("o") or 0.0)
				bm = eff_mult(bo, 0.0, 0.0)
				if bm > 0 and bm >= pm:
					bets.append({
						"outcome": oc,
						"source": bco.get("src") or "BC",
						"sourceKind": "bc",
						"o": bo,
						"c": 0.0,
						"s": 0.0,
					})
				elif po > 0:
					bets.append({
						"outcome": oc,
						"source": "POLYMARKET",
						"sourceKind": "pm",
						"o": po,
						"c": 0.0,
						"s": self.slip,
						"ask": asks[i] if i < len(asks) else 0.0,
					})

			status = "WAIT"
			roi = 0.0
			rr = None
			if (len(bets) == len(m["o"])
					and is_pm_bookmaker_pair(
						bets,
						source_kind_key="sourceKind",
						allow_unregistered_bookmaker=True,
						require_prices=True,
					)):
				rr = compute(bets, self.bank)
				if rr:
					roi = rr["roi"]
					if roi > 0:
						status = "ARB"
					elif roi > -0.002:
						status = "NEAR"
					else:
						status = "OK"
				else:
					status = "WAIT"
			elif any(a > 0 for a in asks):
				status = "OK"
				# PM-only vig proxy
				if all(a > 0 for a in asks):
					roi = 1.0 / sum(asks) - 1.0

			watchlist.append({
				"slug": m["slug"][:26],
				"sport": classify_sport(m["slug"]),
				"roi": roi,
				"status": status,
				"question": m["q"],
			})

			if rr and roi > 0:
				if m["slug"] not in self.first_seen:
					self.first_seen[m["slug"]] = time.time()
					profit_b = rr["payout"] - self.bank
					log_arb(m["slug"], classify_sport(m["slug"]), roi, self.bank, profit_b, bets, rr["stakes"])
					self.log("accent", "ARB +%.3f%% %s" % (roi * 100, m["slug"][:22]))
				age = int(time.time() - self.first_seen[m["slug"]])
				if roi > best_roi:
					best_roi = roi
					legs = []
					for i, b in enumerate(bets):
						legs.append({
							"source": b["source"],
							"sourceKind": b.get("sourceKind") or "pm",
							"outcome": b["outcome"],
							"odds": b["o"],
							"stakeRub": rr["stakes"][i],
						})
					active = {
						"slug": m["slug"],
						"title": m["q"][:48],
						"sport": classify_sport(m["slug"]),
						"meta": "%s · Age %ds · Place both legs NOW" % (classify_sport(m["slug"]), age),
						"roi": roi,
						"ageSec": age,
						"legs": legs,
					}

		uptime_s = int(time.time() - self.started_at)
		uh, um = divmod(uptime_s // 60, 60)
		us = uptime_s % 60
		if uh:
			uptime = "%d:%02d:%02d" % (uh, um, us)
		else:
			uptime = "%d:%02d" % (um, us)

		ws_ready = bool(self.feed and self.feed.ready)
		cfg_out = {
			"bank": self.bank,
			"rate": self.rate,
			"slip": self.slip,
			"poll": self.poll,
			"scan": {
				"top": 800,
				"show": 40,
				"minVolume": 100,
				"minNotional": 50,
				"maxOverround": 1.08,
				"sport": "",
			},
		}
		desk = {
			"wsReady": ws_ready,
			"marketsCount": len(self.markets),
			"uptime": uptime,
			"todayArbs": n_today,
			"todayProfit": p_today,
			"activeArb": active,
			"watchlist": watchlist,
			"feed": list(self.feed_items)[:12],
			"config": cfg_out,
		}
		with self._desk_lock:
			self._desk_cache = desk

	def snapshot(self) -> dict[str, Any]:
		with self._desk_lock:
			if self._desk_cache:
				return dict(self._desk_cache)
		return {
			"wsReady": False,
			"marketsCount": 0,
			"uptime": "0:00",
			"todayArbs": 0,
			"todayProfit": 0,
			"activeArb": None,
			"watchlist": [],
			"feed": list(self.feed_items)[:12],
			"config": {
				"bank": self.bank,
				"rate": self.rate,
				"slip": self.slip,
				"poll": self.poll,
				"scan": {
					"top": 800,
					"show": 40,
					"minVolume": 100,
					"minNotional": 50,
					"maxOverround": 1.08,
					"sport": "",
				},
			},
		}


ENGINE = Monitor()
