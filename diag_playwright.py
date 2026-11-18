# diag_playwright.py — перехват WS-кадров из настоящего Chrome
# Установка: pip install playwright && playwright install chromium
import sys

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    print("pip install playwright && playwright install chromium"); sys.exit(1)

TARGET = "https://betboom.ru/sport/live"     # можно любой live-раздел
WAIT   = 25                                   # секунд держим страницу

def hexs(x, n=48):
    if isinstance(x, bytes): return x[:n].hex()
    return repr(x[:200])

def main():
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=False,
            args=["--disable-blink-features=AutomationControlled"],
        )
        ctx = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36",
            locale="ru-RU",
            viewport={"width": 1366, "height": 900},
        )
        page = ctx.new_page()

        store = {}

        def on_ws(ws):
            print(f"\n[WS OPEN] {ws.url}")
            store[ws.url] = {"recv": [], "sent": []}

            ws.on("framereceived", lambda p: (
                store[ws.url]["recv"].append(p),
                print(f"  recv #{len(store[ws.url]['recv'])} len={len(p)}: {hexs(p)}")
            ))
            ws.on("framesent", lambda p: (
                store[ws.url]["sent"].append(p),
                print(f"  SENT #{len(store[ws.url]['sent'])} len={len(p)}: {hexs(p)}")
            ))
            ws.on("close", lambda: print(f"[WS CLOSE] {ws.url}"))

        page.on("websocket", on_ws)

        print(f"goto {TARGET}")
        page.goto(TARGET, wait_until="domcontentloaded", timeout=30000)
        print(f"staying {WAIT}s ...")
        page.wait_for_timeout(WAIT * 1000)

        print("\n================ SUMMARY ================")
        for url, d in store.items():
            print(f"\nURL: {url}")
            print(f"  recv={len(d['recv'])}  sent={len(d['sent'])}")
            if d["sent"]:
                f = d["sent"][0]
                print(f"  FIRST SENT ({'BIN' if isinstance(f,bytes) else 'TEXT'}): "
                      + (f.hex() if isinstance(f, bytes) else repr(f)))
            if d["recv"]:
                f = d["recv"][0]
                print(f"  FIRST RECV ({'BIN' if isinstance(f,bytes) else 'TEXT'}): "
                      + (f.hex() if isinstance(f, bytes) else repr(f)))

        browser.close()

if __name__ == "__main__":
    main()