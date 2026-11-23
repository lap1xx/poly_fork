#!/usr/bin/env python3
"""
web_app.py - Poly-ARB web radar v4 (СБП-style).

ПОЛОЖИ ЭТОТ ФАЙЛ В КОРЕНЬ ПРОЕКТА (рядом с polyarb_v4.py, bb_bridge.py, config.json).
"""
import asyncio
import csv
import hmac
import io
import ipaddress
import json
import math
import os
import pathlib
import re
import threading
import time
import urllib.parse
import urllib.request
from contextlib import asynccontextmanager
from typing import Set, Dict, List

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from arb_invariants import is_pm_bookmaker_pair
from opportunity import normalize_scanner_snapshot
from pm_fees import fetch_fee_schedule, fee_per_share
from runtime_settings import (
    DEFAULT_RUNTIME_SETTINGS,
    RUNTIME_LIMIT_DEFAULTS,
    normalize_runtime_settings,
    runtime_limits_payload,
    settings_limits_payload,
    validate_setting,
)

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
STATUS_FILE = HERE / "scanner_status.json"
TRADES_FILE = HERE / "trades.jsonl"
HIDE_FILE = HERE / "hidden_slugs.json"
CSV_FILE = HERE / "arbs.csv"
FIRST_SEEN_FILE = HERE / "arb_first_seen.json"
CONFIG_FILE = HERE / "config.json"

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
FIRST_SEEN_TTL = 300.0
DATA_STALE_SEC = RUNTIME_LIMIT_DEFAULTS["dashboard_source_stale_sec"]
SAFE_FORK_THRESHOLD = DEFAULT_RUNTIME_SETTINGS["safe_fork_threshold"]
ALLOWED_ORIGINS = {"http://127.0.0.1:8080", "http://localhost:8080"}

_clients: Set[WebSocket] = set()
_hidden: Set[str] = set()
_first_seen: Dict[str, float] = {}
_first_seen_lock = threading.Lock()
_scanner_state = None
_scanner_session_id = None
_scanner_seq = -1
_broadcast_wakeup = None
SCANNER_WS_MAX_BYTES = 8 * 1024 * 1024
SCANNER_STATE_STALE_SECONDS = 3.0


def _is_valid_scanner_arb(arb):
    """Validate the serialized scanner candidate before it reaches a client."""
    if not isinstance(arb, dict):
        return False
    bookmaker_source = arb.get("bk_source")
    if not isinstance(bookmaker_source, str) or not bookmaker_source.strip():
        return False
    return is_pm_bookmaker_pair(
        arb.get("bets"),
        expected_bookmaker_source=bookmaker_source,
        require_pm_token=True,
        require_prices=True,
    )


def _opportunities_v1_snapshot(raw, scanner_state=None):
    """Build the additive Opportunity v1 view without changing legacy arbs."""
    now = time.time()
    state = scanner_state if isinstance(scanner_state, dict) else {}
    sent_at = _finite_number(state.get("sent_at"), 0.0)
    if sent_at is None:
        sent_at = now
    session_id = state.get("session_id")
    if not isinstance(session_id, str) or not session_id or len(session_id) > 80:
        session_id = "web-app-file-fallback"
    seq = state.get("seq")
    if isinstance(seq, bool) or not isinstance(seq, int) or seq < 1:
        seq = 1
    envelope = {
        "type": "scanner.snapshot",
        "schema_version": 1,
        "scanner_id": "polyarb_v4",
        "session_id": session_id,
        "seq": seq,
        "sent_at": sent_at,
        "arbs": raw if isinstance(raw, dict) else {},
    }
    return normalize_scanner_snapshot(envelope, now_epoch=now)


def _filter_pinned_signal_pairs(scan_status):
    if not isinstance(scan_status, dict):
        return {}
    filtered = dict(scan_status)
    snapshots = filtered.get("pinned_signal_snapshots")
    if isinstance(snapshots, list):
        valid = [row for row in snapshots if _is_valid_scanner_arb(row)]
        filtered["pinned_signal_snapshots"] = valid
        filtered["invalid_pinned_signal_snapshots_dropped"] = len(snapshots) - len(valid)
    return filtered

def _configured_onewin_event_origin():
    """Return a normalized HTTPS origin used by the read-only 1win event links."""
    raw = os.getenv("ONEWIN_ORIGIN", "https://f1w-7707.com").strip()
    try:
        parts = urllib.parse.urlsplit(raw)
        host = (parts.hostname or "").lower().rstrip(".")
        port = parts.port
    except ValueError:
        return ""
    if (parts.scheme.lower() != "https" or not host
            or parts.username or parts.password or port not in (None, 443)
            or parts.path not in ("", "/") or parts.query or parts.fragment
            or not re.fullmatch(r"[a-z0-9.-]+", host)
            or ".." in host or host.startswith(".") or host.endswith(".")):
        return ""
    return f"https://{host}"

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


def _normalize_enabled_bk_sources(value, default=("1win",)):
    if not isinstance(value, (list, tuple, set)):
        value = default
    normalized = set()
    for source in value:
        key = str(source or "").strip().lower().replace(" ", "")
        if key in ("1win", "onewin"):
            normalized.add("1win")
        elif key in ("betboom", "bb"):
            normalized.add("BetBoom")
    return [source for source in ("1win", "BetBoom") if source in normalized]


def _current_scanner_status():
    if isinstance(_scanner_state, dict):
        status = _scanner_state.get("scan_status")
        received = _scanner_state.get("received_at", 0)
        try:
            if isinstance(status, dict) and time.time() - float(received or 0) <= 5.0:
                return dict(status)
        except (TypeError, ValueError, OverflowError):
            pass
    status = _load_json(STATUS_FILE, {})
    return status if isinstance(status, dict) else {}


def _runtime_config_payload():
    cfg = _load_config()
    status = _current_scanner_status()
    running = (
        status.get("state") == "running"
        and _finite_number(status.get("ts"), 0.0, None) is not None
        and time.time() - float(status.get("ts", 0)) <= 5.0
    )
    caps = status.get("available_bk_sources")
    cfg["available_bk_sources"] = (
        _normalize_enabled_bk_sources(caps, default=())
        if isinstance(caps, list) else ["1win", "BetBoom"]
    )
    cfg["scanner_running"] = bool(running)
    cfg["active_bk_sources"] = (
        _normalize_enabled_bk_sources(status.get("enabled_bk_sources"), default=())
        if running else []
    )
    active_roi = _finite_number(status.get("min_roi_pct"), 0.0, 100.0) if running else None
    cfg["active_min_roi_pct"] = (
        active_roi if active_roi is not None else cfg["min_roi_pct"]
    )
    cfg["active_runtime_settings"] = (
        status.get("runtime_settings", {}) if running else {}
    )
    cfg["runtime_limits"] = (
        status.get("runtime_limits", cfg.get("runtime_limits", {}))
        if running else cfg.get("runtime_limits", {})
    )
    cfg["config_warnings"] = (
        status.get("config_warnings", cfg.get("config_warnings", []))
        if running else cfg.get("config_warnings", [])
    )
    return cfg

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
    bot_cfg = cfg.get("bot") if isinstance(cfg.get("bot"), dict) else {}
    scan_cfg = cfg.get("scan") if isinstance(cfg.get("scan"), dict) else {}
    normalized_source = dict(cfg)
    for key in ("bank", "min_roi_pct", "poll", "watch_sec", "alert_gap",
                "safe_fork_threshold", "roi_revision"):
        if key not in normalized_source:
            if key in scan_cfg:
                normalized_source[key] = scan_cfg[key]
            elif key in bot_cfg:
                normalized_source[key] = bot_cfg[key]
    settings, config_warnings, _legacy_migrated = normalize_runtime_settings(
        normalized_source
    )
    runtime_limits = runtime_limits_payload()
    config_warnings.extend(runtime_limits.get("warnings", []))

    sec = cfg.get("secondary_bk")
    if not isinstance(sec, dict):
        sec = {"enabled": False, "name": "1win", "color": "#ff5a5a"}
    else:
        sec.setdefault("enabled", False)
        sec.setdefault("name", "1win")
        sec.setdefault("color", "#ff5a5a")

    enabled_bk_sources = _normalize_enabled_bk_sources(
        cfg.get("enabled_bk_sources", ["1win"])
    ) or ["1win"]

    return {
        "rate": 1.0,
        "bank": settings["bank"],
        "currency": "USDT",
        "poll": settings["poll"],
        "watch_sec": settings["watch_sec"],
        "alert_gap": settings["alert_gap"],
        "safe_fork_threshold": settings["safe_fork_threshold"],
        "min_roi_pct": settings["min_roi_pct"],
        "roi_revision": settings["roi_revision"],
        "enabled_bk_sources": enabled_bk_sources,
        "secondary_bk": sec,
        "config_warnings": config_warnings,
        "config_limits": settings_limits_payload(),
        "runtime_limits": runtime_limits,
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
        bookmaker_age = _finite_number(arb.get("bk_odds_age_sec"), 0.0, 3600.0)
        if source_ts is None or source_ts <= 0:
            if bookmaker_age is None:
                enriched["source_age_sec"] = None
                enriched["source_state"] = "unknown"
            else:
                enriched["source_age_sec"] = round(bookmaker_age, 1)
                enriched["source_state"] = "stale" if bookmaker_age > DATA_STALE_SEC else "fresh"
        else:
            source_age = max(0.0, now - source_ts, bookmaker_age or 0.0)
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

        phase = str(arb.get("phase") or "").strip().lower()
        if phase not in {"live", "prematch", "unknown"}:
            # Legacy snapshots only prove LIVE when the old explicit flag was true.
            # A missing/false flag is not sufficient evidence for PREMATCH.
            phase = "live" if arb.get("is_live") is True else "unknown"
        enriched["phase"] = phase
        enriched["is_live"] = phase == "live"

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
            if not (_home and _away):
                _binding = arb.get("bk_binding") or {}
                _home = (_binding.get("pm_home") or _binding.get("home") or "").strip()
                _away = (_binding.get("pm_away") or _binding.get("away") or "").strip()
            if _home and _away:
                enriched["team1"] = _home
                enriched["team2"] = _away
                enriched["title"] = _home + " vs " + _away
            else:
                enriched["team1"] = ""
                enriched["team2"] = ""
                enriched["title"] = arb.get("q") or arb.get("title") or ""
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
    last_heartbeat = time.time()
    last_scanner_seq = -1
    while True:
        try:
            wakeup = _broadcast_wakeup
            if wakeup is not None:
                try:
                    await asyncio.wait_for(wakeup.wait(), timeout=0.5)
                except asyncio.TimeoutError:
                    pass
                if wakeup.is_set():
                    wakeup.clear()
            now = time.time()
            raw = None
            scan_status = None
            should_broadcast = False
            scanner_state = _scanner_state
            scanner_received_at = (
                scanner_state.get("received_at")
                if isinstance(scanner_state, dict) else None
            )
            scanner_fresh = (
                scanner_received_at is not None
                and now - float(scanner_received_at) <= SCANNER_STATE_STALE_SECONDS
            )
            if scanner_fresh:
                seq = scanner_state.get("seq", -1)
                if seq != last_scanner_seq or now - last_broadcast >= 1.0:
                    raw = scanner_state.get("arbs", {})
                    scan_status = dict(scanner_state.get("scan_status", {}))
                    scan_status["delivery_transport"] = "scanner_ws"
                    scan_status["scanner_state_age_ms"] = round(
                        max(0.0, now - float(scanner_received_at)) * 1000, 1
                    )
                    last_scanner_seq = seq
                    should_broadcast = True
            elif LIVE_FILE.exists():
                st = LIVE_FILE.stat()
                if st.st_mtime > last_ts or now - last_broadcast >= 1.0:
                    last_ts = st.st_mtime
                    raw = _load_json(LIVE_FILE, {})
                    if not isinstance(raw, dict):
                        raw = {}
                    scan_status = _load_json(STATUS_FILE, {})
                    if not isinstance(scan_status, dict):
                        scan_status = {}
                    scan_status.pop("scanner_to_backend_ms", None)
                    scan_status["delivery_transport"] = "file_fallback"
                    should_broadcast = True
            if should_broadcast:
                enriched = _build_enriched(raw or {})
                enriched = {
                    slug: arb for slug, arb in enriched.items()
                    if _is_valid_scanner_arb(arb)
                }
                scan_status = _filter_pinned_signal_pairs(scan_status)
                dropped_count = len(raw or {}) - len(enriched)
                previous_dropped = _finite_number(
                    scan_status.get("invalid_arb_pairs_dropped"), 0, 5000
                ) or 0
                scan_status["invalid_arb_pairs_dropped"] = max(
                    int(previous_dropped), dropped_count
                )
                msg = json.dumps({
                    "type": "snapshot",
                    "ts": time.time(),
                    "arbs": list(enriched.values()),
                    "opportunities_v1": _opportunities_v1_snapshot(
                        raw or {}, scanner_state if scanner_fresh else None
                    ),
                    "total": len(enriched),
                    "hidden_count": len(_hidden),
                    "config": _load_config(),
                    "scan_status": scan_status or {},
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
            if _clients and now - last_heartbeat >= 15.0:
                heartbeat = json.dumps({"type": "heartbeat", "ts": now})
                dead = []
                for ws in list(_clients):
                    try:
                        await ws.send_text(heartbeat)
                    except Exception:
                        dead.append(ws)
                for ws in dead:
                    _clients.discard(ws)
                last_heartbeat = now
        except Exception:
            pass
        await asyncio.sleep(0.5)

@asynccontextmanager
async def _lifespan(app):
    global _broadcast_wakeup
    _broadcast_wakeup = asyncio.Event()
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
        html = html_file.read_text(encoding="utf-8")
        html = html.replace(
            "/*__ONEWIN_EVENT_ORIGIN__*/",
            json.dumps(_configured_onewin_event_origin(), ensure_ascii=True),
        )
        return HTMLResponse(html)
    return HTMLResponse("<h1>Poly-ARB Web v4</h1>")

@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    if ws.headers.get("origin") not in ALLOWED_ORIGINS:
        await ws.close(code=1008)
        return
    await ws.accept()
    _clients.add(ws)
    try:
        if _scanner_state is not None:
            raw = _scanner_state.get("arbs", {})
            scan_status = _scanner_state.get("scan_status", {})
            opportunity_state = _scanner_state
        else:
            raw = _load_json(LIVE_FILE, {})
            scan_status = _load_json(STATUS_FILE, {})
            opportunity_state = None
        if not isinstance(raw, dict):
            raw = {}
        enriched = _build_enriched(raw)
        if not isinstance(scan_status, dict):
            scan_status = {}
        enriched = {
            slug: arb for slug, arb in enriched.items()
            if _is_valid_scanner_arb(arb)
        }
        scan_status = _filter_pinned_signal_pairs(scan_status)
        await ws.send_text(json.dumps({
            "type": "snapshot",
            "ts": time.time(),
            "arbs": list(enriched.values()),
            "opportunities_v1": _opportunities_v1_snapshot(
                raw, opportunity_state
            ),
            "total": len(raw),
            "hidden_count": len(_hidden),
            "config": _load_config(),
            "scan_status": scan_status,
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


def _reject_json_constant(value):
    raise ValueError("non-finite JSON number")


@app.websocket("/ws/scanner")
async def scanner_ingest_ws(ws: WebSocket):
    """Authenticated loopback-only ingest; browsers cannot publish scanner state."""
    global _scanner_state, _scanner_session_id, _scanner_seq
    expected = os.environ.get("POLYARB_SCANNER_WS_TOKEN", "")
    authorization = ws.headers.get("authorization", "")
    origin = ws.headers.get("origin")
    client_host = ws.client.host if ws.client else ""
    try:
        is_loopback = ipaddress.ip_address(client_host).is_loopback
    except ValueError:
        is_loopback = False
    authorized = bool(expected) and hmac.compare_digest(
        authorization, "Bearer " + expected
    )
    if (not authorized or not is_loopback
            or (origin is not None and origin not in ALLOWED_ORIGINS)):
        await ws.close(code=1008)
        return

    await ws.accept()
    try:
        while True:
            raw_message = await ws.receive_text()
            if len(raw_message.encode("utf-8")) > SCANNER_WS_MAX_BYTES:
                await ws.close(code=1009)
                return
            try:
                payload = json.loads(raw_message, parse_constant=_reject_json_constant)
            except (TypeError, ValueError):
                await ws.close(code=1003)
                return
            if not isinstance(payload, dict):
                await ws.close(code=1003)
                return
            session_id = payload.get("session_id")
            seq = payload.get("seq")
            sent_at = payload.get("sent_at")
            arbs = payload.get("arbs")
            scan_status = payload.get("scan_status")
            if (payload.get("type") != "scanner.snapshot"
                    or payload.get("schema_version") != 1
                    or payload.get("scanner_id") != "polyarb_v4"
                    or not isinstance(session_id, str) or not session_id
                    or len(session_id) > 80
                    or isinstance(seq, bool) or not isinstance(seq, int) or seq < 1
                    or not isinstance(sent_at, (int, float))
                    or not math.isfinite(float(sent_at))
                    or abs(time.time() - float(sent_at)) > 60.0
                    or not isinstance(arbs, dict) or len(arbs) > 5000
                    or not isinstance(scan_status, dict)):
                await ws.close(code=1008)
                return
            valid_arbs = {}
            invalid_arb_pairs = 0
            for slug, arb in arbs.items():
                if (not isinstance(slug, str) or not slug or len(slug) > 512
                        or not isinstance(arb, dict)):
                    valid_arbs = None
                    break
                if not _is_valid_scanner_arb(arb):
                    invalid_arb_pairs += 1
                    continue
                valid_arbs[slug] = arb
            if valid_arbs is None:
                await ws.close(code=1008)
                return
            if session_id != _scanner_session_id:
                _scanner_session_id = session_id
                _scanner_seq = -1
            if seq <= _scanner_seq:
                continue
            _scanner_seq = seq
            scan_status = dict(scan_status)
            scan_status["invalid_arb_pairs_dropped"] = invalid_arb_pairs
            scan_status["scanner_to_backend_ms"] = round(
                max(0.0, time.time() - float(sent_at)) * 1000, 1
            )
            _scanner_state = {
                "session_id": session_id,
                "seq": seq,
                "sent_at": float(sent_at),
                "received_at": time.time(),
                "arbs": valid_arbs,
                "scan_status": scan_status,
            }
            if _broadcast_wakeup is not None:
                _broadcast_wakeup.set()
    except WebSocketDisconnect:
        pass
    except Exception:
        pass

@app.get("/api/config")
async def api_config():
    cfg = _runtime_config_payload()
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
        cfg.pop("pm_fee_pct", None)
        normalized_source = dict(cfg)
        bot_cfg = cfg.get("bot") if isinstance(cfg.get("bot"), dict) else {}
        scan_cfg = cfg.get("scan") if isinstance(cfg.get("scan"), dict) else {}
        for key in ("bank", "min_roi_pct", "poll", "watch_sec", "alert_gap",
                    "safe_fork_threshold", "roi_revision"):
            if key not in normalized_source:
                if key in scan_cfg:
                    normalized_source[key] = scan_cfg[key]
                elif key in bot_cfg:
                    normalized_source[key] = bot_cfg[key]
        normalized_settings, normalization_warnings, _legacy_migrated = (
            normalize_runtime_settings(normalized_source)
        )
        cfg.update(normalized_settings)
        cfg["currency"] = "USDT"
        cfg["rate"] = 1.0
        for k in ("bank", "safe_fork_threshold", "min_roi_pct", "poll",
                  "watch_sec", "alert_gap"):
            if k in payload:
                try:
                    value = validate_setting(payload[k], k)
                except ValueError as exc:
                    return JSONResponse({"ok": False, "err": str(exc)},
                                        status_code=422)
                cfg[k] = value
        if "enabled_bk_sources" in payload:
            raw_sources = payload["enabled_bk_sources"]
            if (not isinstance(raw_sources, list) or len(raw_sources) > 2
                    or any(not isinstance(source, str) for source in raw_sources)):
                return JSONResponse({"ok": False, "err": "enabled_bk_sources must be a list"},
                                    status_code=422)
            enabled_sources = _normalize_enabled_bk_sources(raw_sources, default=())
            if (not enabled_sources or any(
                    not _normalize_enabled_bk_sources([source], default=())
                    for source in raw_sources)):
                return JSONResponse({"ok": False, "err": "select at least one supported bookmaker"},
                                    status_code=422)
            status = _current_scanner_status()
            running = status.get("state") == "running" and (
                _finite_number(status.get("ts"), 0.0, None) is not None
                and time.time() - float(status.get("ts", 0)) <= 5.0
            )
            available = status.get("available_bk_sources")
            if running and isinstance(available, list):
                available_sources = set(
                    _normalize_enabled_bk_sources(available, default=())
                )
                if not set(enabled_sources).issubset(available_sources):
                    return JSONResponse({
                        "ok": False,
                        "err": "Один из источников недоступен в текущем запуске сканера",
                    }, status_code=409)
            cfg["enabled_bk_sources"] = enabled_sources
        if "min_roi_pct" in payload:
            try:
                cfg["roi_revision"] = max(0, int(cfg.get("roi_revision", 0))) + 1
            except (TypeError, ValueError):
                cfg["roi_revision"] = 1
        cfg["currency"] = "USDT"
        cfg["rate"] = 1.0
        _save_json(CONFIG_FILE, cfg)
        result_config = _runtime_config_payload()
        if normalization_warnings:
            result_config["config_warnings"] = list(
                result_config.get("config_warnings", [])
            ) + normalization_warnings
        return {"ok": True, "config": result_config}
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


def _trade_amount_usdt(trade, field):
    """Normalize legacy RUB records with their saved FX rate; new records are USDT."""
    amount = _finite_number(trade.get(field), -1_000_000_000.0, 1_000_000_000.0)
    if amount is None:
        return None
    currency = str(trade.get("currency") or "RUB").upper()
    if currency == "USDT":
        return amount
    if currency != "RUB":
        return None
    rate = _finite_number(trade.get("rate"), 0.01, 100_000.0)
    return amount / rate if rate else None

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
    currency = str(payload.get("currency") or "RUB").upper()
    legs = payload.get("legs")
    if (None in (bank, profit, roi, payout, rate)
            or currency not in {"USDT", "RUB"}
            or not isinstance(legs, list) or len(legs) != 2):
        return JSONResponse({"ok": False, "err": "invalid trade fields"}, status_code=422)
    clean_legs = []
    numeric_leg_fields = {
        "odds": (1.000001, 1_000_000.0),
        "stake_rub": (0.01, 1_000_000_000.0),
        "stake_usdt": (0.01, 1_000_000_000.0),
        "price_usd": (0.000001, 0.999999), "amount_usd": (0.01, 1_000_000_000.0),
        "amount_usdt": (0.01, 1_000_000_000.0),
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
    if not is_pm_bookmaker_pair(clean_legs, source_key="book"):
        return JSONResponse({
            "ok": False,
            "err": "trade must contain one Polymarket leg and one bookmaker leg with opposite outcomes",
        }, status_code=422)
    pm_leg = next(leg for leg in clean_legs if leg["book"].strip().casefold() == "polymarket")
    bookmaker_leg = next(leg for leg in clean_legs if leg is not pm_leg)
    pm_price = _finite_number(pm_leg.get("price_usd"), 0.000001, 0.999999)
    bookmaker_odds = _finite_number(bookmaker_leg.get("odds"), 1.000001, 1_000_000.0)
    if pm_price is None or bookmaker_odds is None:
        return JSONResponse({
            "ok": False,
            "err": "trade requires a Polymarket price and bookmaker odds",
        }, status_code=422)
    rec = {
        "slug": slug.strip(),
        "q": payload.get("q", "") if isinstance(payload.get("q", ""), str) else "",
        "sport": payload.get("sport", "") if isinstance(payload.get("sport", ""), str) else "",
        "market_type": payload.get("market_type", "") if isinstance(payload.get("market_type", ""), str) else "",
        "bank": bank, "profit": profit, "roi": roi, "payout": payout,
        "rate": rate, "currency": currency,
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
    normalized = []
    for trade in trades:
        bank_usdt = _trade_amount_usdt(trade, "bank")
        profit_usdt = _trade_amount_usdt(trade, "profit")
        if bank_usdt is not None and profit_usdt is not None:
            normalized.append((bank_usdt, profit_usdt))
    n = len(normalized)
    bank_sum = sum(row[0] for row in normalized)
    profit_sum = sum(row[1] for row in normalized)
    roi = (profit_sum / bank_sum) if bank_sum > 0 else 0.0
    return {
        "date": date, "count": n, "currency": "USDT",
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
        normalized = [
            (_trade_amount_usdt(t, "bank"), _trade_amount_usdt(t, "profit"))
            for t in trades
        ]
        normalized = [row for row in normalized if None not in row]
        bank = sum(row[0] for row in normalized)
        profit = sum(row[1] for row in normalized)
        roi = (profit / bank) if bank > 0 else 0.0
        out.append({"date": d, "roi": roi, "profit": profit, "count": len(normalized), "currency": "USDT"})
    return {"days": out}

@app.get("/api/export_csv")
async def api_export_csv():
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(["ts", "slug", "q", "sport", "market_type", "currency", "bank", "profit", "roi"])
    if not TRADES_FILE.exists():
        return PlainTextResponse(output.getvalue(), media_type="text/csv")
    for line in TRADES_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
            r.setdefault("currency", "RUB")
            writer.writerow([_csv_safe(r.get(key, "")) for key in
                             ("ts", "slug", "q", "sport", "market_type", "currency", "bank", "profit", "roi")])
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


def _walk_pm_asks_with_fee(asks, cash_budget, schedule):
    """Estimate shares purchasable for a cash budget using a market fee curve."""
    remaining = float(cash_budget)
    gross_cost = fee_total = shares = 0.0
    for price, available in asks:
        fee_ps = fee_per_share(price, schedule["rate"], schedule["exponent"])
        if fee_ps is None:
            return None
        cash_per_share = price + fee_ps
        take = min(available, remaining / cash_per_share)
        if take <= 0:
            continue
        shares += take
        gross_cost += take * price
        fee_total += take * fee_ps
        remaining -= take * cash_per_share
        if remaining <= max(1e-8, cash_budget * 1e-10):
            break
    if shares <= 0:
        return None
    total_cost = gross_cost + fee_total
    return {
        "avg_price": gross_cost / shares,
        "shares": shares,
        "fee": fee_total,
        "total_cost": total_cost,
        "odds_eff": shares / total_cost,
        "full": remaining <= max(1e-8, cash_budget * 1e-10),
    }


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
        token_ids = mk.get("clobTokenIds", "[]")
        outcomes = mk.get("outcomes", "[]")
        token_ids = json.loads(token_ids) if isinstance(token_ids, str) else token_ids
        outcomes = json.loads(outcomes) if isinstance(outcomes, str) else outcomes
        condition_id = str(mk.get("conditionId") or "").strip()
        if not condition_id:
            return {"ok": False, "err": "fee_schedule_unavailable"}
        fee_schedule = fetch_fee_schedule(condition_id, force=bool(force))
        if fee_schedule is None:
            return {"ok": False, "err": "fee_schedule_unavailable"}
        if not isinstance(token_ids, list) or not isinstance(outcomes, list):
            return {"ok": False, "err": "invalid_market_metadata"}
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
                if math.isfinite(p) and math.isfinite(s) and 0 < p < 1 and s > 0:
                    bids.append((p, s))
            except Exception:
                pass
        for x in (b.get("asks") or []):
            try:
                p, s = float(x["price"]), float(x["size"])
                if math.isfinite(p) and math.isfinite(s) and 0 < p < 1 and s > 0:
                    asks.append((p, s))
            except Exception:
                pass
        bids.sort(key=lambda z: -z[0])
        asks.sort(key=lambda z: z[0])

        best_bid = bids[0][0] if bids else None
        best_ask = asks[0][0] if asks else None
        ask_depth_usd = sum(
            (p + (fee_per_share(p, fee_schedule["rate"], fee_schedule["exponent"]) or 0.0)) * s
            for p, s in asks
        )
        quote = _walk_pm_asks_with_fee(asks, size_usd, fee_schedule)

        sizes_rows = []
        for sz in (10, 20, 50, 100, 200):
            quote2 = _walk_pm_asks_with_fee(asks, sz, fee_schedule)
            if quote2:
                sizes_rows.append({
                    "size": sz,
                    "avg_price": round(quote2["avg_price"], 4),
                    "odds_eff": round(quote2["odds_eff"], 4),
                    "fee_estimate": round(quote2["fee"], 6),
                    "full": quote2["full"],
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
                {"avg_price": round(quote["avg_price"], 4),
                 "odds_eff": round(quote["odds_eff"], 4),
                 "fee_estimate": round(quote["fee"], 6),
                 "shares": round(quote["shares"], 6),
                 "full": quote["full"]}
                if quote else None
            ),
        })
    return {"ok": True, "fee_schedule": fee_schedule, "books": books}

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
