"""
chrome_bridge.py - BetBoom tree_ws relay via real Chrome.

Chrome opens a WS to ru-ws2.sporthub.bet (Cloudflare lets real Chrome through),
we hook that WS from inside the page, and mirror every frame to a local
server on 127.0.0.1:8765. bb_bridge.py connects to the same local server.
Every frame from any client is broadcast to all OTHER clients.

    [Chrome page] --wss--> [BetBoom]
         |  ^
         v  | mirror frames via local WS
    [chrome_bridge :8765] <--ws-- [bb_bridge.py]

USAGE
    pip install websockets playwright
    playwright install chromium

    Terminal 1:  python chrome_bridge.py
    Terminal 2:  python -B bot_live.py
"""

import argparse
import asyncio
import threading
import time

import websockets
from playwright.sync_api import sync_playwright

HOST = "127.0.0.1"
PORT = 8765
BB_URL = "https://betboom.ru/sport/esports"
BB_URLS = [
    "https://betboom.ru/sport/esports",
    "https://betboom.ru/sport/tennis",
    "https://betboom.ru/sport/live/esports",
    "https://betboom.ru/sport/live/tennis",
]

INIT_JS = r"""
(() => {
  if (window.__bb_bridge_installed) return;
  window.__bb_bridge_installed = true;

  const Orig = window.WebSocket;
  const LOCAL_URL = "ws://localhost:8765/";
  const log = (...a) => console.log('[bb-bridge]', ...a);

  function Patched(url, protocols) {
    const ws = new Orig(url, protocols);
    try {
      if (typeof url === 'string' && url.indexOf('/tree_ws/') !== -1) {
        log('hooked tree_ws:', url);
        window.__bb_tree_ws = ws;
        ws.binaryType = 'arraybuffer';

        ws.addEventListener('open', () => {
          log('tree_ws open, connecting local relay');
          let local;
          try {
            local = new Orig(LOCAL_URL);
          } catch (e) { log('local connect failed', e); return; }
          local.binaryType = 'arraybuffer';

          local.addEventListener('open', () => log('local relay connected'));
          local.addEventListener('close', () => log('local relay closed'));
          local.addEventListener('error', (e) => log('local err', e));

          ws.addEventListener('message', (ev) => {
            if (local.readyState === 1) {
              try { local.send(ev.data); } catch (e) {}
            }
          });

          local.addEventListener('message', (ev) => {
            if (ws.readyState === 1) {
              try { ws.send(ev.data); } catch (e) {}
            }
          });
        });
      }
    } catch (e) { log('hook err', e); }
    return ws;
  }

  Patched.prototype = Orig.prototype;
  ['CONNECTING','OPEN','CLOSING','CLOSED'].forEach(k => Patched[k] = Orig[k]);
  window.WebSocket = Patched;
  log('WebSocket hook installed');
})();
"""

class Relay:
    """Local WS server. Broadcast every frame to all OTHER clients."""

    def __init__(self, port):
        self.port = port
        self.clients = set()
        self._stat = {"in": 0, "out": 0}

    async def _handler(self, ws):
        peer = ws.remote_address
        self.clients.add(ws)
        print(f"[bridge] client connected: {peer}  (total={len(self.clients)})")
        try:
            async for msg in ws:
                if isinstance(msg, str):
                    msg = msg.encode()
                self._stat["in"] += 1
                for c in list(self.clients):
                    if c is ws:
                        continue
                    try:
                        await c.send(msg)
                        self._stat["out"] += 1
                    except Exception:
                        pass
        except websockets.ConnectionClosed:
            pass
        finally:
            self.clients.discard(ws)
            print(f"[bridge] client disconnected: {peer}  (total={len(self.clients)})")

    async def _serve(self):
        async with websockets.serve(
            self._handler, HOST, self.port,
            max_size=None,
        ):
            print(f"[bridge] relay listening on ws://{HOST}:{self.port}")
            while True:
                await asyncio.sleep(15)
                print(f"[bridge] stats: in={self._stat['in']} "
                      f"out={self._stat['out']} clients={len(self.clients)}")

    def start(self):
        def _run():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            loop.run_until_complete(self._serve())
        threading.Thread(target=_run, daemon=True).start()
        time.sleep(0.5)

def _find_betboom_page(ctx):
    for pg in ctx.pages:
        try:
            if "betboom.ru" in pg.url:
                return pg
        except Exception:
            continue
    return None


def _hook_alive(page):
    try:
        return bool(page.evaluate("() => !!window.__bb_bridge_installed"))
    except Exception:
        return False


def _ensure_tree_ws(page, timeout_ms=30000):
    try:
        page.wait_for_function("window.__bb_tree_ws !== undefined",
                               timeout=timeout_ms)
        return True
    except Exception:
        return False


def _attach_or_open(ctx):
    """Find existing betboom page or open one, install hook, confirm tree_ws."""
    page = _find_betboom_page(ctx)
    if page is None:
        print("[bridge] no betboom tab found, opening new ...", flush=True)
        page = ctx.new_page()
        try:
            page.goto(BB_URL, wait_until="domcontentloaded", timeout=60000)
        except Exception as e:
            print(f"[bridge] new page goto failed: {e}", flush=True)

    if not _hook_alive(page):
        print("[bridge] installing hook + reloading ...", flush=True)
        try:
            page.goto("https://betboom.ru/esport/live/dota-2",
                      wait_until="domcontentloaded", timeout=60000)
        except Exception as e:
            print(f"[bridge] goto failed: {e}", flush=True)
    else:
        print("[bridge] hook already alive", flush=True)

    def _console(msg):
        if "[bb-bridge]" in msg.text:
            print(f"[chrome] {msg.text}", flush=True)
    try:
        page.on("console", _console)
    except Exception:
        pass

    print(f"[bridge] attached to page: {page.url}", flush=True)
    if _ensure_tree_ws(page, timeout_ms=30000):
        print("[bridge] tree_ws hook confirmed", flush=True)
    else:
        print("[bridge] WARN: hook not confirmed on attach", flush=True)
    return page


def run_chrome(headless: bool):
    print("[bridge] connecting to real Chrome on :9222 ...", flush=True)
    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp("http://127.0.0.1:9222")
        ctx = browser.contexts[0]
        ctx.add_init_script(INIT_JS)

        page = None
        consecutive_fails = 0
        last_healthcheck = 0.0
        HEALTH_EVERY = 30.0

        print("[bridge] running with watchdog. Ctrl-C to stop.", flush=True)

        while True:
            try:
                # ---- (re)attach phase ----
                if page is None or page.is_closed():
                    page = _attach_or_open(ctx)
                    consecutive_fails = 0
                    last_healthcheck = time.time()

                # ---- main tick ----
                page.wait_for_timeout(1000)

                # ---- periodic healthcheck ----
                now = time.time()
                if now - last_healthcheck >= HEALTH_EVERY:
                    last_healthcheck = now
                    if page.is_closed():
                        print("[bridge] page closed (tick check)", flush=True)
                        page = None
                        continue
                    if not _hook_alive(page):
                        print("[bridge] hook lost, forcing reload ...", flush=True)
                        try:
                            page.goto("https://betboom.ru/esport/live/dota-2",
                                      wait_until="domcontentloaded", timeout=60000)
                            _ensure_tree_ws(page, timeout_ms=30000)
                        except Exception as e:
                            print(f"[bridge] reload failed: {e}", flush=True)
                            page = None

            except Exception as e:
                consecutive_fails += 1
                backoff = min(2.0 * consecutive_fails, 15.0)
                print(f"[bridge] watchdog caught {type(e).__name__}: {e}", flush=True)
                print(f"[bridge] reattaching in {backoff:.1f}s ...", flush=True)
                page = None
                time.sleep(backoff)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--headless", action="store_true")
    ap.add_argument("--port", type=int, default=PORT)
    args = ap.parse_args()

    Relay(args.port).start()
    run_chrome(headless=args.headless)

if __name__ == "__main__":
    main()