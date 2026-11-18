# diag_ws_lib.py — тест через websockets (другой TLS-стек, с permessage-deflate)
import asyncio
import sys

try:
    import websockets
except ImportError:
    print("pip install websockets"); sys.exit(1)

URL = "wss://ru-ws2.sporthub.bet/api/tree_ws/v1"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36",
    "Origin": "https://betboom.ru",
    "Referer": "https://betboom.ru/",
    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
}

async def main():
    try:
        async with websockets.connect(
            URL,
            additional_headers=HEADERS,   # на websockets<12 — extra_headers
            subprotocols=["protobuf"],
            compression="deflate",
            open_timeout=10,
        ) as ws:
            print("connected, subprotocol:", ws.subprotocol)
            for i in range(15):
                try:
                    m = await asyncio.wait_for(ws.recv(), timeout=10)
                except asyncio.TimeoutError:
                    print("timeout"); break
                except Exception as e:
                    print(f"recv err: {type(e).__name__}: {e}"); break
                if isinstance(m, bytes):
                    print(f"#{i+1} BIN({len(m)}): {m[:40].hex()}")
                else:
                    print(f"#{i+1} TEXT({len(m)}): {m[:200]!r}")
    except Exception as e:
        print(f"connect fail: {type(e).__name__}: {e}")

asyncio.run(main())