# diag_now.py — три проверки одной командой
import asyncio, ssl, socket, time
import requests

print("=== T1. TCP/TLS к ru-ws2.sporthub.bet:443 ===")
try:
    t0 = time.time()
    s = socket.create_connection(("ru-ws2.sporthub.bet", 443), timeout=5)
    ctx = ssl.create_default_context()
    ss = ctx.wrap_socket(s, server_hostname="ru-ws2.sporthub.bet")
    print(f"OK TLS in {time.time()-t0:.2f}s proto={ss.version()} alpn={ss.selected_alpn_protocol()!r}")
    ss.close()
except Exception as e:
    print(f"FAIL: {type(e).__name__}: {e}")

print("\n=== T2. HTTP siteapi.betboom.ru (работает ли HTTP вообще) ===")
try:
    r = requests.post(
        "https://siteapi.betboom.ru/api/site_api/v1/sporthub/tree/get_match_details_info",
        json={"match_id": 5922175, "language": "LANGUAGES_RU", "theme": "THEMES_BLACK"},
        headers={
            "Origin": "https://betboom.ru",
            "Referer": "https://betboom.ru/",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36",
            "x-platform": "web_mobile",
            "Content-Type": "application/json",
        },
        timeout=10,
    )
    print(f"status={r.status_code} len={len(r.text)} first120={r.text[:120]!r}")
except Exception as e:
    print(f"FAIL: {type(e).__name__}: {e}")

print("\n=== T3. Playwright: идут ли кадры СЕЙЧАС ===")
try:
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        b = p.chromium.launch(headless=False,
                              args=["--disable-blink-features=AutomationControlled"])
        ctx = b.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36",
            locale="ru-RU", viewport={"width": 1366, "height": 900},
        )
        pg = ctx.new_page()
        cnt = {"n": 0}
        def on_ws(ws):
            if "tree_ws" not in ws.url: return
            print(f"[WS] {ws.url}")
            ws.on("framereceived", lambda p: (cnt.__setitem__("n", cnt["n"]+1),
                                              print(f"  recv#{cnt['n']} len={len(p)}") if cnt['n']<=5 else None))
        pg.on("websocket", on_ws)
        pg.goto("https://betboom.ru/sport/live", wait_until="domcontentloaded")
        pg.wait_for_timeout(15000)
        print(f"TOTAL frames: {cnt['n']}")
        b.close()
except Exception as e:
    print(f"FAIL: {type(e).__name__}: {e}")