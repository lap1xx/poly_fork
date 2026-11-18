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
print('opened. Echo mode: respond TEXT with TEXT""')
ws.settimeout(15.0)
t0 = time.time()
n = 0
while time.time() - t0 < 15:
    try:
        m = ws.recv()
        n += 1
        if isinstance(m, str):
            print(f'  [{time.time()-t0:5.1f}s] #{n} TEXT({len(m)}): {repr(m[:200])}')
            try:
                ws.send('')
                print(f'  [{time.time()-t0:5.1f}s]   -> sent TEXT("")')
            except Exception as e:
                print(f'  echo err: {e}')
                break
        elif isinstance(m, bytes):
            print(f'  [{time.time()-t0:5.1f}s] #{n} BIN({len(m)}): {m[:40].hex()}')
    except websocket.WebSocketTimeoutException:
        print(f'  [{time.time()-t0:5.1f}s] timeout')
        break
    except Exception as e:
        print(f'  [{time.time()-t0:5.1f}s] ERR: {type(e).__name__}: {e}')
        break
print(f'total received: {n}')
try: ws.close()
except: pass