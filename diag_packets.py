# diag_packets.py - test which packet kills the WS
import websocket, time

def vint(x):
    out = b""
    while True:
        b = x & 0x7f; x >>= 7
        out += bytes([b | 0x80 if x else b])
        if not x: break
    return out

def tag(f, wt): return vint((f << 3) | wt)
def s(f, v):    b = v.encode(); return tag(f, 2) + vint(len(b)) + b
def enum(f, v): return tag(f, 0) + vint(v)
def msg(f, p):  return tag(f, 2) + vint(len(p)) + p

URL = 'wss://ru-ws2.sporthub.bet:443/api/tree_ws/v1'
HDR = {
    'User-Agent':'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36',
    'Referer':'https://betboom.ru/'
}

def connect():
    return websocket.create_connection(
        URL, timeout=15, subprotocols=['protobuf'],
        origin='https://betboom.ru', header=HDR,
    )

def try_packet(name, payload):
    print(f"=== {name} ===")
    try:
        ws = connect()
    except Exception as e:
        print("  connect err:", type(e).__name__, e)
        return
    time.sleep(0.3)
    try:
        # read initial hello if any
        ws.settimeout(1.0)
        try:
            hi = ws.recv()
            print(f"  server hello: {len(hi)}B  hex={hi[:20].hex()}")
        except Exception:
            print("  server hello: (none)")
        ws.settimeout(5.0)
        ws.send_binary(payload)
        print(f"  sent {len(payload)}B: {payload[:20].hex()}")
        time.sleep(0.5)
        print(f"  alive 0.5s: {ws.connected}")
        try:
            r = ws.recv()
            print(f"  response: {len(r)}B  hex={r[:20].hex()}")
        except Exception as e:
            print(f"  recv err: {type(e).__name__}: {e}")
        time.sleep(3)
        print(f"  alive 3.5s: {ws.connected}")
    except Exception as e:
        print(f"  send err: {type(e).__name__}: {e}")
    try: ws.close()
    except Exception: pass
    print()

# 1. Пакет, который шлём СЕЙЧАС первым
try_packet("TEST A: f3 settings (current code)",
           msg(3, enum(1, 8) + s(2, "1_hours")))

# 2. Пустой пинг (f1 = 0)
try_packet("TEST B: f1 ping varint 0",
           enum(1, 0))

# 3. Пинг через msg(f1, b"") — пустая строка
try_packet("TEST C: f1 ping empty string",
           msg(1, b""))

# 4. Просто ничего не слать
try_packet("TEST D: send nothing (empty bytes)",
           b"\x08\x00")