# diag_chrome_ws.py — снять handshake WS + все cookies из живого Chrome
from playwright.sync_api import sync_playwright

TARGET = "https://betboom.ru/sport/live"
WAIT_SEC = 20

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

        # CDP-сессия для перехвата network-событий
        cdp = ctx.new_cdp_session(page)
        cdp.send("Network.enable")

        seen = set()

        def on_ws_handshake(params):
            req = params.get("request", {})
            url = req.get("url", "")
            if "sporthub.bet" not in url:
                return
            if url in seen:
                return
            seen.add(url)
            print(f"\n{'='*72}")
            print(f"WS HANDSHAKE REQUEST")
            print(f"  URL:    {url}")
            print(f"  Method: {req.get('method')}")
            print(f"  Headers:")
            for k, v in sorted(req.get("headers", {}).items()):
                print(f"    {k}: {v}")
            # Тоже посмотрим какие subprotocols попросили
            ext = req.get("headers", {}).get("Sec-WebSocket-Extensions", "")
            proto = req.get("headers", {}).get("Sec-WebSocket-Protocol", "")
            print(f"  [parsed] Sec-WebSocket-Protocol: {proto!r}")
            print(f"  [parsed] Sec-WebSocket-Extensions: {ext!r}")

        def on_ws_created(params):
            url = params.get("url", "")
            if "sporthub.bet" in url:
                print(f"\n[WS CREATED] {url}")

        cdp.on("Network.webSocketWillSendHandshakeRequest", on_ws_handshake)
        cdp.on("Network.webSocketCreated", on_ws_created)

        print(f"Opening {TARGET} ...")
        page.goto(TARGET, wait_until="domcontentloaded", timeout=30000)
        print(f"Waiting {WAIT_SEC}s for WS traffic ...\n")
        page.wait_for_timeout(WAIT_SEC * 1000)

        print(f"\n{'='*72}")
        print("ALL COOKIES")
        print(f"{'='*72}")
        for c in ctx.cookies():
            val = c["value"]
            if len(val) > 90:
                val = val[:90] + "..."
            print(f"  domain={c['domain']:35s} name={c['name']:32s} = {val}")

        browser.close()

if __name__ == "__main__":
    main()