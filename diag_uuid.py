# diag_uuid.py — проверяем гипотезу ?uuid=
import asyncio
import os
import time
import uuid
import websockets

URL_BASE = "wss://ru-ws2.sporthub.bet/api/tree_ws/v1"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36",
    "Origin": "https://betboom.ru",
    "Referer": "https://betboom.ru/",
    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
    "Cache-Control": "no-cache",
}

def uuid7() -> str:
    """UUIDv7 — time-ordered, как у Chrome/бандла betboom."""
    ts_ms = int(time.time() * 1000)
    rand_a = int.from_bytes(os.urandom(2), "big") & 0x0FFF
    rand_b = int.from_bytes(os.urandom(8), "big") & 0x3FFF_FFFF_FFFF_FFFF
    ts_hi = (ts_ms >> 16) & 0xFFFF_FFFF
    ts_lo = ts_ms & 0xFFFF
    n = (ts_hi << 96) | (ts_lo << 80) | (0x7 << 76) | (rand_a << 64) | (0x2 << 62) | rand_b
    return str(uuid.UUID(int=n))

def short_uid(n: int = 6) -> str:
    """Короткий hex-uid как в f1 settings (напр. 'cca31f')."""
    return os.urandom(n // 2 + 1).hex()[:n]

def settings_f3(uid: str) -> bytes:
    """MainRequest.f3 = { f1: uid, f2: 'all', f3: 8 }"""
    uid_b = uid.encode()
    # f1 (tag 0x0a) + len + uid
    inner = bytes([0x0A, len(uid_b)]) + uid_b
    # f2 (tag 0x12) + len 3 + 'all'
    inner += bytes([0x12, 0x03]) + b"all"
    # f3 (tag 0x18) + varint 8
    inner += bytes([0x18, 0x08])
    # wrap in f3 (tag 0x1a) + len
    return bytes([0x1A, len(inner)]) + inner

async def main():
    sid = uuid7()
    url = f"{URL_BASE}?uuid={sid}"
    print(f"URL: {url}")

    async with websockets.connect(
        url, additional_headers=HEADERS,
        subprotocols=["protobuf"],
        compression="deflate",
        open_timeout=10,
    ) as ws:
        print(f"CONNECTED  subprotocol={ws.subprotocol}")
        uid = short_uid()
        sf = settings_f3(uid)
        print(f"SEND settings f3 (uid={uid}): {sf.hex()}")
        await ws.send(sf)

        got_bin = 0
        got_text = 0
        for i in range(40):
            try:
                m = await asyncio.wait_for(ws.recv(), timeout=10)
            except asyncio.TimeoutError:
                print("timeout"); break
            except Exception as e:
                print(f"recv err: {type(e).__name__}: {e}"); break
            if isinstance(m, bytes):
                got_bin += 1
                print(f"#{i+1} BIN({len(m)}): {m[:48].hex()}")
            else:
                got_text += 1
                print(f"#{i+1} TEXT({len(m)}): {m[:160]!r}")
        print(f"\nTOTAL bin={got_bin} text={got_text}")

asyncio.run(main())