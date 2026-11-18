# diag_ws_headers.py — перебор заголовков через websockets
import asyncio
import websockets

URL = "wss://ru-ws2.sporthub.bet/api/tree_ws/v1"

BASE = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36",
    "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept-Encoding": "gzip, deflate, br, zstd",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "websocket",
    "Sec-Fetch-Site": "cross-site",
}

# Сюда вставь Cookie из DevTools, если он там есть. Оставь "" если нет.
COOKIE = ""

VARIANTS = [
    ("A betboom.ru/ base",      {"Origin": "https://betboom.ru", "Referer": "https://betboom.ru/"}),
    ("B sportbook origin",      {"Origin": "https://sportbook.sporthub.bet", "Referer": "https://sportbook.sporthub.bet/"}),
    ("C betboom live ref",      {"Origin": "https://betboom.ru", "Referer": "https://betboom.ru/sport/live"}),
    ("D betboom esport ref",    {"Origin": "https://betboom.ru", "Referer": "https://betboom.ru/esport"}),
    ("E same-site cross-orig",  {"Origin": "https://betboom.ru", "Referer": "https://ru-ws2.sporthub.bet/"}),
]

async def one(name, extra, use_proto=True):
    h = {**BASE, **extra}
    if COOKIE:
        h["Cookie"] = COOKIE
    try:
        kwargs = dict(additional_headers=h, compression="deflate", open_timeout=10)
        if use_proto:
            kwargs["subprotocols"] = ["protobuf"]
        async with websockets.connect(URL, **kwargs) as ws:
            print(f"[{name}] CONNECTED proto={ws.subprotocol}")
            try:
                m = await asyncio.wait_for(ws.recv(), timeout=5)
                if isinstance(m, bytes):
                    print(f"[{name}]   first BIN({len(m)}): {m[:40].hex()}")
                else:
                    print(f"[{name}]   first TEXT({len(m)}): {m[:100]!r}")
            except asyncio.TimeoutError:
                print(f"[{name}]   connected, no frame in 5s")
            except Exception as e:
                print(f"[{name}]   recv err: {type(e).__name__}: {e}")
    except Exception as e:
        print(f"[{name}] connect err: {type(e).__name__}: {e}")

async def main():
    for name, extra in VARIANTS:
        await one(name, extra)
    await one("F no-subprotocol", VARIANTS[0][1], use_proto=False)

asyncio.run(main())