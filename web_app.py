#!/usr/bin/env python3
"""
web_app.py - Poly-ARB web radar v4 (СБП-style).

ПОЛОЖИ ЭТОТ ФАЙЛ В КОРЕНЬ ПРОЕКТА (рядом с polyarb_v4.py, bb_bridge.py, config.json).
"""
import asyncio
import csv
import io
import json
import math
import os
import pathlib
import threading
import time
import urllib.parse
import urllib.request
from contextlib import asynccontextmanager
from typing import Set, Dict, List

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

try:
    import price_history
except Exception:
    price_history = None

try:
    import bk_adapter
except Exception:
    bk_adapter = None

HERE = pathlib.Path(__file__).parent
LIVE_FILE = HERE / "arbs_live.json"
TRADES_FILE = HERE / "trades.jsonl"
HIDE_FILE = HERE / "hidden_slugs.json"
CSV_FILE = HERE / "arbs.csv"
FIRST_SEEN_FILE = HERE / "arb_first_seen.json"
CONFIG_FILE = HERE / "config.json"

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
PM_FEE = 0.04
FIRST_SEEN_TTL = 300.0
DATA_STALE_SEC = 10.0
SAFE_FORK_THRESHOLD = 0.45
ALLOWED_ORIGINS = {"http://127.0.0.1:8080", "http://localhost:8080"}

_clients: Set[WebSocket] = set()
_hidden: Set[str] = set()
_first_seen: Dict[str, float] = {}
_first_seen_lock = threading.Lock()

def _load_json(path: pathlib.Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default

def _save_json(path: pathlib.Path, data):
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)
    except Exception:
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass

def _finite_number(value, minimum=None, maximum=None):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    if minimum is not None and number < minimum:
        return None
    if maximum is not None and number > maximum:
        return None
    return number

def _csv_safe(value):
    if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@", "\t", "\r")):
        return "'" + value
    return value

def _load_hidden():
    _hidden.update(_load_json(HIDE_FILE, []))

def _save_hidden():
    _save_json(HIDE_FILE, sorted(_hidden))

def _load_first_seen():
    raw = _load_json(FIRST_SEEN_FILE, {})
    if isinstance(raw, dict):
        with _first_seen_lock:
            for k, v in raw.items():
                try:
                    _first_seen[k] = float(v)
                except Exception:
                    continue

def _save_first_seen():
    with _first_seen_lock:
        snap = dict(_first_seen)
    _save_json(FIRST_SEEN_FILE, snap)

def _load_config():
    """Loads config.json. Supports both v1 flat and v2 nested (scan/bot)."""
    cfg = _load_json(CONFIG_FILE, {})
    if not isinstance(cfg, dict):
        cfg = {}

    rate = _finite_number(cfg.get("rate"), 0.01, 100_000.0) or 85.0
    bank = _finite_number(cfg.get("bank"), 1.0, 1_000_000_000.0) or 2500.0

    bot_cfg = cfg.get("bot") if isinstance(cfg.get("bot"), dict) else {}
    poll = _finite_number((bot_cfg or {}).get("poll", cfg.get("poll", 0.3)),
                          0.05, 30.0) or 0.3

    sec = cfg.get("secondary_bk")
    if not isinstance(sec, dict):
        sec = {"enabled": False, "name": "1win", "color": "#ff5a5a"}
    else:
        sec.setdefault("enabled", False)
        sec.setdefault("name", "1win")
        sec.setdefault("color", "#ff5a5a")

    pm_fee_pct = _finite_number(cfg.get("pm_fee_pct", PM_FEE), 0.0, 1.0)
    if pm_fee_pct is None:
        pm_fee_pct = PM_FEE
    safe_threshold = _finite_number(
        cfg.get("safe_fork_threshold", SAFE_FORK_THRESHOLD), 0.01, 0.99
    )
    if safe_threshold is None:
        safe_threshold = SAFE_FORK_THRESHOLD

    return {
        "rate": rate,
        "bank": bank,
        "poll": poll,
        "pm_fee_pct": pm_fee_pct,
        "safe_fork_threshold": safe_threshold,
        "secondary_bk": sec,
    }

_load_hidden()
_load_first_seen()

def _http_get(url, params=None, timeout=8):
    if params:
        url = url + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": "poly-arb/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))

def _first_seen_loop():
    while True:
        try:
            if LIVE_FILE.exists():
                raw = LIVE_FILE.read_text(encoding="utf-8")
                data = json.loads(raw) if raw else {}
                if isinstance(data, dict):
                    now = time.time()
                    with _first_seen_lock:
                        for slug in data.keys():
                            if slug not in _first_seen:
                                _first_seen[slug] = now
                        dead = [
                            s for s, ts in _first_seen.items()
                            if now - ts > FIRST_SEEN_TTL and s not in data
                        ]
                        for s in dead:
                            _first_seen.pop(s, None)
        except Exception:
            pass
        time.sleep(2.0)

def _first_seen_save_loop():
    while True:
        _save_first_seen()
        time.sleep(20.0)

def _build_enriched(raw: dict) -> dict:
    cfg = _load_config()
    safe_thr = cfg["safe_fork_threshold"]
    now = time.time()
    out = {}
    sec = bk_adapter.get_secondary() if bk_adapter else None
    sec_enabled = bk_adapter.is_secondary_enabled() if bk_adapter else False
    sec_name = sec.name if sec else "1win"
    sec_bindings = sec.bindings() if sec else {}

    with _first_seen_lock:
        fs_snapshot = dict(_first_seen)

    for slug, arb in raw.items():
        if slug in _hidden:
            continue
        if not isinstance(arb, dict):
            continue
        enriched = dict(arb)
        enriched["slug"] = slug
        enriched["first_seen_ts"] = fs_snapshot.get(slug, now)
        enriched["age_sec"] = round(now - enriched["first_seen_ts"], 1)
        source_ts = _finite_number(arb.get("ts"), 0.0)
        if source_ts is None or source_ts <= 0:
            enriched["source_age_sec"] = None
            enriched["source_state"] = "unknown"
        else:
            source_age = max(0.0, now - source_ts)
            enriched["source_age_sec"] = round(source_age, 1)
            enriched["source_state"] = "stale" if source_age > DATA_STALE_SEC else "fresh"

        pm_best_ask = arb.get("pm_best_ask")
        pm_best_bid = arb.get("pm_best_bid")
        bets = arb.get("bets") or []
        if not isinstance(bets, list):
            bets = []
        for b in bets:
            if not isinstance(b, dict):
                continue
            if b.get("source") == "Polymarket":
                if pm_best_ask is None:
                    pm_best_ask = b.get("pm_best_ask")
                if pm_best_bid is None:
                    pm_best_bid = b.get("pm_best_bid")
                break
        try:
            pm_best_ask = float(pm_best_ask)
            if not (0.0 < pm_best_ask < 1.0):
                pm_best_ask = None
        except (TypeError, ValueError):
            pm_best_ask = None
        try:
            pm_best_bid = float(pm_best_bid)
            if not (0.0 < pm_best_bid < 1.0):
                pm_best_bid = None
        except (TypeError, ValueError):
            pm_best_bid = None
        enriched["pm_best_ask"] = round(pm_best_ask, 4) if pm_best_ask is not None else None
        enriched["pm_best_bid"] = round(pm_best_bid, 4) if pm_best_bid is not None else None
        # A low token price is only a filter flag, never a guarantee of settlement.
        enriched["pm_low_price_flag"] = bool(
            pm_best_ask is not None and pm_best_ask <= safe_thr
        )
        enriched["is_safe"] = False

        phase = arb.get("phase")
        if not phase:
            phase = "live" if arb.get("is_live") else "prematch"
        enriched["phase"] = phase

        enriched["secondary_bk"] = {
            "name": sec_name,
            "enabled": sec_enabled,
            "bound": slug in sec_bindings,
        }
        # --- FIX: команды для TOTAL-вилок (у PM в q нет команд) ---
        try:
            _bbdbg = arb.get("bb_stakes_debug") or []
            _home = ""
            _away = ""
            for _s in _bbdbg:
                if not isinstance(_s, dict):
                    continue
                _sn = (_s.get("short_name") or "").strip().lower()
                _nm = (_s.get("name") or "").strip()
                if not _nm:
                    continue
                if _sn in ("win1", "п1") and not _home:
                    _home = _nm
                elif _sn in ("win2", "п2") and not _away:
                    _away = _nm
            if _home and _away:
                enriched["team1"] = _home
                enriched["team2"] = _away
                enriched["title"] = _home + " vs " + _away
            else:
                enriched["team1"] = ""
                enriched["team2"] = ""
                enriched["title"] = ""
        except Exception:
            enriched["team1"] = ""
            enriched["team2"] = ""
            enriched["title"] = ""
        # --- /FIX ---
        out[slug] = enriched
    return out

async def _broadcast_loop():
    last_ts = 0.0
    last_broadcast = 0.0
    while True:
        try:
            if LIVE_FILE.exists():
                st = LIVE_FILE.stat()
                now = time.time()
                if st.st_mtime > last_ts or now - last_broadcast >= 1.0:
                    last_ts = st.st_mtime
                    raw = _load_json(LIVE_FILE, {})
                    if not isinstance(raw, dict):
                        raw = {}
                    enriched = _build_enriched(raw)
                    msg = json.dumps({
                        "type": "snapshot",
                        "ts": time.time(),
                        "arbs": list(enriched.values()),
                        "total": len(raw),
                        "hidden_count": len(_hidden),
                        "config": _load_config(),
                    }, ensure_ascii=False)
                    dead = []
                    for ws in list(_clients):
                        try:
                            await ws.send_text(msg)
                        except Exception:
                            dead.append(ws)
                    for ws in dead:
                        _clients.discard(ws)
                    last_broadcast = now
        except Exception:
            pass
        await asyncio.sleep(0.5)

@asynccontextmanager
async def _lifespan(app):
    asyncio.create_task(_broadcast_loop())
    threading.Thread(target=_first_seen_loop, daemon=True).start()
    threading.Thread(target=_first_seen_save_loop, daemon=True).start()
    if price_history is not None:
        price_history.start()
    try:
        yield
    finally:
        if price_history is not None:
            price_history.stop()

app = FastAPI(title="Poly-ARB Web v4", lifespan=_lifespan)

@app.middleware("http")
async def _same_origin_mutations(request: Request, call_next):
    if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
        origin = request.headers.get("origin")
        host = request.headers.get("host", "").split(":", 1)[0].strip("[]").lower()
        client_host = (request.client.host if request.client else "").lower()
        if (origin not in ALLOWED_ORIGINS
                or host not in {"127.0.0.1", "localhost"}
                or client_host not in {"127.0.0.1", "::1", "localhost"}):
            return JSONResponse({"ok": False, "err": "local same-origin request required"},
                                status_code=403)
    return await call_next(request)

STATIC = HERE / "static"
TEMPLATES = HERE / "templates"
if STATIC.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")

@app.get("/")
async def index():
    html_file = TEMPLATES / "index.html"
    if html_file.exists():
        return HTMLResponse(html_file.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>Poly-ARB Web v4</h1>")

@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    if ws.headers.get("origin") not in ALLOWED_ORIGINS:
        await ws.close(code=1008)
        return
    await ws.accept()
    _clients.add(ws)
    try:
        raw = _load_json(LIVE_FILE, {})
        if not isinstance(raw, dict):
            raw = {}
        enriched = _build_enriched(raw)
        await ws.send_text(json.dumps({
            "type": "snapshot",
            "ts": time.time(),
            "arbs": list(enriched.values()),
            "total": len(raw),
            "hidden_count": len(_hidden),
            "config": _load_config(),
        }, ensure_ascii=False))
    except Exception:
        pass
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        _clients.discard(ws)

@app.get("/api/config")
async def api_config():
    cfg = _load_config()
    sec_enabled = bk_adapter.is_secondary_enabled() if bk_adapter else False
    cfg["secondary_bk"]["enabled"] = sec_enabled
    return cfg

@app.post("/api/config")
async def api_config_save(payload: dict):
    try:
        if not isinstance(payload, dict):
            return JSONResponse({"ok": False, "err": "object required"}, status_code=422)
        cfg = _load_json(CONFIG_FILE, {}) or {}
        if not isinstance(cfg, dict):
            cfg = {}
        limits = {
            "bank": (1.0, 1_000_000_000.0),
            "rate": (0.01, 100_000.0),
            "pm_fee_pct": (0.0, 1.0),
            "safe_fork_threshold": (0.01, 0.99),
        }
        for k, (minimum, maximum) in limits.items():
            if k in payload:
                value = _finite_number(payload[k], minimum, maximum)
                if value is None:
                    return JSONResponse({"ok": False, "err": "invalid " + k},
                                        status_code=422)
                cfg[k] = value
        _save_json(CONFIG_FILE, cfg)
        return {"ok": True, "config": _load_config()}
    except Exception as e:
        return JSONResponse({"ok": False, "err": str(e)}, status_code=500)

@app.post("/api/hide/{slug}")
async def api_hide(slug: str):
    if not slug or len(slug) > 300 or any(ord(ch) < 32 for ch in slug):
        return JSONResponse({"ok": False, "err": "invalid slug"}, status_code=422)
    _hidden.add(slug)
    _save_hidden()
    return {"ok": True, "hidden": sorted(_hidden)}

@app.post("/api/unhide/{slug}")
async def api_unhide(slug: str):
    if not slug or len(slug) > 300 or any(ord(ch) < 32 for ch in slug):
        return JSONResponse({"ok": False, "err": "invalid slug"}, status_code=422)
    _hidden.discard(slug)
    _save_hidden()
    return {"ok": True, "hidden": sorted(_hidden)}

@app.post("/api/trade")
async def api_trade(payload: dict):
    if not isinstance(payload, dict):
        return JSONResponse({"ok": False, "err": "object required"}, status_code=422)
    slug = payload.get("slug")
    if (not isinstance(slug, str) or not slug.strip() or len(slug) > 300
            or any(ord(ch) < 32 for ch in slug)):
        return JSONResponse({"ok": False, "err": "invalid slug"}, status_code=422)
    bank = _finite_number(payload.get("bank"), 0.01, 1_000_000_000.0)
    profit = _finite_number(payload.get("profit"), -1_000_000_000.0, 1_000_000_000.0)
    roi = _finite_number(payload.get("roi"), -1.0, 1000.0)
    payout = _finite_number(payload.get("payout"), 0.0, 1_000_000_000.0)
    rate = _finite_number(payload.get("rate"), 0.01, 100_000.0)
    legs = payload.get("legs")
    if None in (bank, profit, roi, payout, rate) or not isinstance(legs, list) or len(legs) != 2:
        return JSONResponse({"ok": False, "err": "invalid trade fields"}, status_code=422)
    clean_legs = []
    numeric_leg_fields = {
        "odds": (1.000001, 1_000_000.0), "stake_rub": (0.01, 1_000_000_000.0),
        "price_usd": (0.000001, 0.999999), "amount_usd": (0.01, 1_000_000_000.0),
    }
    for leg in legs:
        if not isinstance(leg, dict):
            return JSONResponse({"ok": False, "err": "invalid leg"}, status_code=422)
        clean_leg = {}
        for key in ("book", "outcome"):
            value = leg.get(key, "")
            if not isinstance(value, str) or len(value) > 120 or any(ord(ch) < 32 for ch in value):
                return JSONResponse({"ok": False, "err": "invalid leg text"}, status_code=422)
            clean_leg[key] = value
        for key, (minimum, maximum) in numeric_leg_fields.items():
            if key in leg:
                value = _finite_number(leg[key], minimum, maximum)
                if value is None:
                    return JSONResponse({"ok": False, "err": "invalid leg " + key},
                                        status_code=422)
                clean_leg[key] = value
        clean_legs.append(clean_leg)
    rec = {
        "slug": slug.strip(),
        "q": payload.get("q", "") if isinstance(payload.get("q", ""), str) else "",
        "sport": payload.get("sport", "") if isinstance(payload.get("sport", ""), str) else "",
        "market_type": payload.get("market_type", "") if isinstance(payload.get("market_type", ""), str) else "",
        "bank": bank, "profit": profit, "roi": roi, "payout": payout, "rate": rate,
        "legs": clean_legs,
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    for key in ("q", "sport", "market_type"):
        rec[key] = rec[key][:300]
    try:
        with open(TRADES_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception as e:
        return JSONResponse({"ok": False, "err": str(e)}, status_code=500)
    return {"ok": True, "saved": rec}

@app.get("/api/trades")
async def api_trades(limit: int = 0):
    out: List[dict] = []
    if TRADES_FILE.exists():
        try:
            for line in TRADES_FILE.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except Exception:
                    continue
        except Exception:
            pass
    out.sort(key=lambda r: r.get("ts", ""), reverse=True)
    if limit > 0:
        out = out[:limit]
    return {"trades": out}

@app.get("/api/daily")
async def api_daily(date: str = ""):
    if not date:
        date = time.strftime("%Y-%m-%d", time.gmtime())
    trades = []
    if TRADES_FILE.exists():
        for line in TRADES_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                if rec.get("ts", "").startswith(date):
                    trades.append(rec)
            except Exception:
                continue
    n = len(trades)
    bank_sum = sum(float(t.get("bank", 0)) for t in trades)
    profit_sum = sum(float(t.get("profit", 0)) for t in trades)
    roi = (profit_sum / bank_sum) if bank_sum > 0 else 0.0
    return {
        "date": date, "count": n,
        "bank": bank_sum, "profit": profit_sum, "roi": roi,
    }

@app.get("/api/weekly")
async def api_weekly():
    from datetime import datetime, timedelta, timezone
    out = []
    today = datetime.now(timezone.utc).date()
    for i in range(6, -1, -1):
        d = (today - timedelta(days=i)).isoformat()
        trades = []
        if TRADES_FILE.exists():
            for line in TRADES_FILE.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                    if rec.get("ts", "").startswith(d):
                        trades.append(rec)
                except Exception:
                    continue
        bank = sum(float(t.get("bank", 0)) for t in trades)
        profit = sum(float(t.get("profit", 0)) for t in trades)
        roi = (profit / bank) if bank > 0 else 0.0
        out.append({"date": d, "roi": roi, "profit": profit, "count": len(trades)})
    return {"days": out}

@app.get("/api/export_csv")
async def api_export_csv():
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(["ts", "slug", "q", "sport", "market_type", "bank", "profit", "roi"])
    if not TRADES_FILE.exists():
        return PlainTextResponse(output.getvalue(), media_type="text/csv")
    for line in TRADES_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
            writer.writerow([_csv_safe(r.get(key, "")) for key in
                             ("ts", "slug", "q", "sport", "market_type", "bank", "profit", "roi")])
        except Exception:
            continue
    return PlainTextResponse(output.getvalue(), media_type="text/csv",
                             headers={"Content-Disposition": "attachment; filename=arbs.csv"})

@app.get("/api/price_history/{slug}")
async def api_price_history(slug: str, max_points: int = 120):
    if price_history is None:
        return {"ok": False, "err": "price_history not loaded", "points": []}
    pts = price_history.get_history(slug, max_points=max_points)
    return {"ok": True, "slug": slug, "points": pts}

@app.get("/api/pm_url/{slug}")
async def api_pm_url(slug: str):
    return {"url": f"https://polymarket.com/event/{slug}"}

@app.get("/api/pm_book/{slug}")
async def api_pm_book(slug: str, size_usd: float = 50.0, force: int = 0):
    size_usd = _finite_number(size_usd, 1.0, 100_000.0)
    if size_usd is None or not slug or len(slug) > 300:
        return JSONResponse({"ok": False, "err": "invalid request"}, status_code=422)
    try:
        m = _http_get(f"{GAMMA}/markets", {"slug": slug})
        if not m or not isinstance(m, list) or not m:
            return {"ok": False, "err": "market not found"}
        mk = m[0]
        token_ids = json.loads(mk.get("clobTokenIds", "[]"))
        outcomes = json.loads(mk.get("outcomes", "[]"))
    except Exception as e:
        return {"ok": False, "err": f"gamma: {e}"}

    books = []
    for i, tid in enumerate(token_ids[:2]):
        outcome = outcomes[i] if i < len(outcomes) else f"out{i}"
        try:
            b = _http_get(f"{CLOB}/book", {"token_id": tid})
        except Exception as e:
            books.append({
                "outcome": outcome, "best_bid": None, "best_ask": None,
                "ask_depth_usd": 0, "bids": [], "asks": [],
                "sizes": [], "avg_for_size": None, "err": str(e),
            })
            continue

        bids, asks = [], []
        for x in (b.get("bids") or []):
            try:
                p, s = float(x["price"]), float(x["size"])
                if p > 0 and s > 0:
                    bids.append((p, s))
            except Exception:
                pass
        for x in (b.get("asks") or []):
            try:
                p, s = float(x["price"]), float(x["size"])
                if p > 0 and s > 0:
                    asks.append((p, s))
            except Exception:
                pass
        bids.sort(key=lambda z: -z[0])
        asks.sort(key=lambda z: z[0])

        best_bid = bids[0][0] if bids else None
        best_ask = asks[0][0] if asks else None
        ask_depth_usd = sum(p * s for p, s in asks)

        avg_price = None
        avg_full = False
        rem = size_usd
        spent = 0.0
        shares = 0.0
        for p, s in asks:
            lvl_usd = p * s
            take = min(rem, lvl_usd)
            if take <= 0:
                continue
            shares += take / p
            spent += take
            rem -= take
            if rem <= 1e-6:
                break
        if shares > 0:
            avg_price = spent / shares
            avg_full = rem <= 1e-6

        sizes_rows = []
        for sz in (10, 20, 50, 100, 200):
            rem2 = sz
            spent2 = 0.0
            sh2 = 0.0
            for p, s in asks:
                lvl_usd = p * s
                take = min(rem2, lvl_usd)
                if take <= 0:
                    continue
                sh2 += take / p
                spent2 += take
                rem2 -= take
                if rem2 <= 1e-6:
                    break
            if sh2 > 0:
                ap = spent2 / sh2
                sizes_rows.append({
                    "size": sz,
                    "avg_price": round(ap, 4),
                    "odds_eff": round((1 / ap) / (1 + PM_FEE), 4),
                    "full": rem2 <= 1e-6,
                })
            else:
                sizes_rows.append({"size": sz, "avg_price": None, "odds_eff": None, "full": False})

        books.append({
            "outcome": outcome,
            "best_bid": best_bid,
            "best_ask": best_ask,
            "ask_depth_usd": round(ask_depth_usd, 2),
            "bids": [{"price": p, "size": s} for p, s in bids[:10]],
            "asks": [{"price": p, "size": s} for p, s in asks[:10]],
            "sizes": sizes_rows,
            "avg_for_size": (
                {"avg_price": round(avg_price, 4),
                 "odds_eff": round((1 / avg_price) / (1 + PM_FEE), 4),
                 "full": avg_full}
                if avg_price else None
            ),
        })
    return {"ok": True, "books": books}

@app.post("/api/shutdown")
async def api_shutdown():
    import os, signal
    def _kill():
        time.sleep(0.5)
        try:
            os.kill(os.getpid(), signal.SIGTERM)
        except Exception:
            pass
    threading.Thread(target=_kill, daemon=True).start()
    return {"ok": True, "msg": "shutting down"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8080, log_level="warning")
