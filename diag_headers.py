import websocket

URL = 'wss://ru-ws2.sporthub.bet:443/api/tree_ws/v1'
HDR = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36',
    'Accept-Language': 'ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7',
    'Accept-Encoding': 'gzip, deflate, br, zstd',
    'Cache-Control': 'no-cache',
    'Pragma': 'no-cache',
    'Sec-Fetch-Dest': 'empty',
    'Sec-Fetch-Mode': 'websocket',
    'Sec-Fetch-Site': 'same-site',
    'Referer': 'https://betboom.ru/',
    'Origin': 'https://betboom.ru',
}
ws = websocket.create_connection(
    URL, timeout=10, subprotocols=['protobuf'],
    origin='https://betboom.ru', header=HDR,
)
print('opened with full browser headers')
ws.settimeout(10.0)
try:
    m = ws.recv()
    print(f'#1: {type(m).__name__} len={len(m) if m else 0}')
    for i in range(5):
        try:
            m = ws.recv()
            if isinstance(m, bytes):
                print(f'#{i+2} BIN({len(m)}): {m[:32].hex()}')
            else:
                print(f'#{i+2} TEXT({len(m)}): {m[:100]!r}')
        except Exception as e:
            print(f'#{i+2} err: {type(e).__name__}: {e}')
            break
except Exception as e:
    print(f'#1 err: {type(e).__name__}: {e}')
try: ws.close()
except: pass