# diag_cookie.py — WS с cookies
import asyncio, os, time, uuid, websockets

URL_BASE = "wss://ru-ws2.sporthub.bet/api/tree_ws/v1"

# ВСТАВЬ СЮДА строку cookies из DevTools (пример: "cf_clearance=abc...; session=xyz...")
COOKIE = ""

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36",
    "Origin": "https://betboom.ru",
    "Referer": "https://betboom.ru/sport/live",
    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
    "Cookie": COOKIE,
}

def uuid7() -> str:
    ts_ms = int(time.time() * 1000)
    rand_a = int.from_bytes(os.urandom(2), "big") & 0x0FFF
    rand_b = int.from_bytes(os.urandom(8), "big") & 0x3FFF_FFFF_FFFF_FFFF
    ts_hi = (ts_ms >> 16) & 0xFFFF_FFFF
    ts_lo = ts_ms & 0xFFFF
    n = (ts_hi << 96) | (ts_lo << 80) | (0x7 << 76) | (rand_a << 64) | (0x2 << 62) | rand_b
    return str(uuid.UUID(int=n))

def settings_f3(uid: str) -> bytes:
    uid_b = uid.encode()
    inner = bytes([0x0A, len(uid_b)]) + uid_b
    inner += bytes([0x12, 0x03]) + b"all"
    inner += bytes([0x18, 0x08])
    return bytes([0x1A, len(inner)]) + inner

async def main():
    url = f"{URL_BASE}?uuid={uuid7()}"
    print(f"URL: {url}")
    print(f"Cookie len={len(COOKIE)}")
    async with websockets.connect(
        url, additional_headers=HEADERS,
        subprotocols=["protobuf"], compression="deflate",
        open_timeout=10,
    ) as ws:
        print(f"CONNECTED proto={ws.subprotocol}")
        uid = os.urandom(3).hex()
        await ws.send(settings_f3(uid))
        print(f"SENT settings f3 uid={uid}")
        for i in range(30):
            try:
                m = await asyncio.wait_for(ws.recv(), timeout=10)
            except asyncio.TimeoutError:
                print("timeout"); break
            except Exception as e:
                print(f"recv err: {type(e).__name__}: {e}"); break
            if isinstance(m, bytes):
                print(f"#{i+1} BIN({len(m)}): {m[:48].hex()}")
            else:
                print(f"#{i+1} TEXT({len(m)}): {m[:160]!r}")

asyncio.run(main())