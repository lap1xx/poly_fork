import websocket, time

URL = 'wss://ru-ws2.sporthub.bet:443/api/tree_ws/v1'
HDR = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36',
    'Referer': 'https://betboom.ru/',
}
ws = websocket.create_connection(
    URL, timeout=10, subprotocols=['protobuf'],
    origin='https://betboom.ru', header=HDR,
)
print('opened, subprotocol:', ws.subprotocol)
ws.settimeout(10.0)
try:
    m = ws.recv()
    print(f'#1 recv: type={type(m).__name__} len={len(m) if m else 0}')
except Exception as e:
    print(f'#1 recv err: {type(e).__name__}: {e}')

# Now try to read again and catch close reason
try:
    m = ws.recv()
    print(f'#2 recv: {m!r}')
except Exception as e:
    print(f'#2 recv err: {type(e).__name__}: {e}')

# Close codes (attributes set after close)
try:
    print(f'close_status_code: {ws.status}')
except Exception as e: print('status?', e)
try:
    print(f'close_reason: {ws.close_reason if hasattr(ws, "close_reason") else "n/a"}')
except Exception as e: print('reason?', e)
try:
    print(f'sock closed: {ws.sock}')  # может None
except Exception as e: print('sock?', e)

try: ws.close()
except: pass