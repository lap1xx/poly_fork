import websocket, time

URL = 'wss://ru-ws2.sporthub.bet:443/api/tree_ws/v1'
HDR = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36',
    'Referer': 'https://betboom.ru/'
}
ws = websocket.create_connection(
    URL, timeout=10, subprotocols=['protobuf'],
    origin='https://betboom.ru', header=HDR,
)
print('opened. Reading only, never sending.')
ws.settimeout(20.0)
t0 = time.time()
n = 0
while time.time() - t0 < 20:
    try:
        m = ws.recv()
    except websocket.WebSocketTimeoutException:
        print(f'  [{time.time()-t0:.1f}s] timeout')
        break
    except Exception as e:
        print(f'  [{time.time()-t0:.1f}s] ERR: {type(e).__name__}: {e}')
        break
    n += 1
    if isinstance(m, str):
        print(f'  [{time.time()-t0:5.1f}s] #{n} TEXT({len(m)}): {m[:200]!r}')
    elif isinstance(m, bytes):
        print(f'  [{time.time()-t0:5.1f}s] #{n} BIN({len(m)}): {m[:40].hex()}')
    else:
        print(f'  [{time.time()-t0:5.1f}s] #{n} ??? {type(m)}')
print(f'total: {n}')
try: ws.close()
except: pass