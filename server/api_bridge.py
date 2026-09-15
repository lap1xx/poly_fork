#!/usr/bin/env python3
"""API for Telegram Mini App + live Polymarket monitor."""
from __future__ import annotations

import json
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from server.monitor import (
	ENGINE,
	ROOT,
	load_cfg,
	load_watch_slugs,
	save_watch_slugs,
)

CFG_FILE = ROOT / "config.json"
DB_FILE = ROOT / "arbs.db"
BC_FILE = ROOT / "bc_odds.json"


@asynccontextmanager
async def lifespan(_app: FastAPI):
	ENGINE.start()
	yield
	ENGINE.stop_all()


app = FastAPI(title="PolyFork Mini App API", version="0.2.0", lifespan=lifespan)
app.add_middleware(
	CORSMiddleware,
	allow_origins=["*"],
	allow_methods=["*"],
	allow_headers=["*"],
)


class ConfigIn(BaseModel):
	bank: float
	rate: float
	slip: float
	poll: float
	scan: dict[str, Any] | None = None


class BcOddsIn(BaseModel):
	rows: list[dict[str, Any]]


class WatchIn(BaseModel):
	slugs: list[str]


@app.get("/api/health")
def health():
	snap = ENGINE.snapshot()
	return {
		"ok": True,
		"wsReady": snap.get("wsReady"),
		"markets": snap.get("marketsCount"),
		"hasWsLib": True,
	}


@app.get("/api/config")
def get_config():
	c = load_cfg()
	bot = c.get("bot", {})
	scan = c.get("scan", {})
	rate = c.get("rate") or ENGINE.rate
	return {
		"bank": c.get("bank", 20000),
		"rate": rate,
		"slip": bot.get("slip", 0),
		"poll": bot.get("poll", 1),
		"scan": {
			"top": scan.get("top", 800),
			"show": scan.get("show", 40),
			"minVolume": scan.get("min_volume", 100),
			"minNotional": scan.get("min_notional", 50),
			"maxOverround": scan.get("max_overround", 1.08),
			"sport": scan.get("sport", ""),
		},
	}


@app.post("/api/config")
def post_config(body: ConfigIn):
	c = load_cfg()
	c["bank"] = body.bank
	c["rate"] = body.rate
	c.setdefault("bot", {})
	c["bot"]["slip"] = body.slip
	c["bot"]["poll"] = body.poll
	if body.scan:
		c["scan"] = {
			"top": body.scan.get("top", 800),
			"show": body.scan.get("show", 40),
			"min_volume": body.scan.get("minVolume", 100),
			"min_notional": body.scan.get("minNotional", 50),
			"max_overround": body.scan.get("maxOverround", 1.08),
			"sport": body.scan.get("sport", ""),
			"tier": "",
		}
	CFG_FILE.write_text(json.dumps(c, indent=2, ensure_ascii=False), encoding="utf-8")
	ENGINE.bank = body.bank
	ENGINE.slip = body.slip
	ENGINE.poll = body.poll
	if body.rate > 0:
		ENGINE.rate = body.rate
	ENGINE.log("accent", "config updated")
	return {"ok": True}


@app.post("/api/bc-odds")
def post_bc_odds(body: BcOddsIn):
	BC_FILE.write_text(json.dumps(body.rows, indent=2, ensure_ascii=False), encoding="utf-8")
	ENGINE.log("accent", "BC odds saved · %d rows" % len(body.rows))
	return {"ok": True, "n": len(body.rows)}


@app.get("/api/bc-odds")
def get_bc_odds():
	if not BC_FILE.exists():
		return []
	try:
		return json.loads(BC_FILE.read_text(encoding="utf-8"))
	except Exception:
		return []


@app.get("/api/watch")
def get_watch():
	return {"slugs": load_watch_slugs()}


@app.post("/api/watch")
def post_watch(body: WatchIn):
	save_watch_slugs(body.slugs)
	ENGINE.log("accent", "watchlist %d · restart API to reload markets" % len(body.slugs))
	return {"ok": True}


@app.get("/api/history")
def history():
	if not DB_FILE.exists():
		return []
	import sqlite3

	con = sqlite3.connect(DB_FILE)
	cur = con.execute(
		"SELECT ts, slug, sport, roi, profit, src1, out1, src2, out2 "
		"FROM arbs ORDER BY ts DESC LIMIT 50"
	)
	rows = [
		{
			"ts": r[0],
			"slug": r[1],
			"sport": r[2],
			"roi": r[3],
			"profit": r[4],
			"src1": r[5],
			"out1": r[6],
			"src2": r[7],
			"out2": r[8],
		}
		for r in cur.fetchall()
	]
	con.close()
	return rows


@app.get("/api/desk")
def desk():
	return ENGINE.snapshot()


@app.get("/api/scan")
def scan():
	path = ROOT / "2way_candidates.txt"
	if not path.exists():
		return []
	out = []
	lines = path.read_text(encoding="utf-8").splitlines()[1:]
	for line in lines[:40]:
		parts = line.split("\t")
		if len(parts) < 10:
			continue
		out.append(
			{
				"slug": parts[8],
				"sport": parts[0],
				"tier": parts[1],
				"market": parts[2],
				"overround": float(parts[3]),
				"volume24h": float(parts[4] or 0),
				"question": parts[9],
				"asks": [0.5, 0.5],
				"outcomes": ["A", "B"],
			}
		)
	return out


@app.get("/api/live")
def live():
	# lightweight: expose markets with live asks from books
	rows = []
	for m in ENGINE.markets:
		asks = []
		for i, oc in enumerate(m["o"]):
			tid = m["t"][i]
			a = ENGINE.best_ask(tid)
			asks.append({
				"outcome": oc,
				"ask": a,
				"k": (1.0 / a) if a > 0 else 0.0,
			})
		rows.append({
			"slug": m["slug"],
			"sport": m["slug"].split("-")[0].upper(),
			"title": m["q"],
			"score": "",
			"period": "WATCH",
			"asks": asks,
		})
	return rows
