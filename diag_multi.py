import websocket, time

URL = 'wss://ru-ws2.sporthub.bet:443/api/tree_ws/v1'
HDR = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36',
    'Referer': 'https://betboom.ru/'
}

def test(name, on_first_msg):
    print(f'=== {name} ===')
    try:
        ws = websocket.create_connection(
            URL, timeout=10, subprotocols=['protobuf'],
            origin='https://betboom.ru', header=HDR,
        )
    except Exception as e:
        print(f'  connect fail: {e}')
        return
    ws.settimeout(8.0)
    t0 = time.time()
    got = 0
    try:
        while time.time() - t0 < 8:
            try:
                m = ws.recv()
            except websocket.WebSocketTimeoutException:
                print(f'  [{time.time()-t0:.1f}s] timeout (no more data)')
                break
            got += 1
            if got == 1:
                if isinstance(m, str):
                    print(f'  #1 TEXT({len(m)}) -> {on_first_msg.__name__}')
                    try: on_first_msg(ws, m)
                    except Exception as e: print(f'  reply err: {e}'); break
                else:
                    print(f'  #1 BIN({len(m)}) hex={m[:40].hex()}')
            else:
                if isinstance(m, str):
                    print(f'  #{got} TEXT({len(m)}): {m[:80]!r}')
                else:
                    print(f'  #{got} BIN({len(m)}): {m[:40].hex()}')
    except Exception as e:
        print(f'  closed: {type(e).__name__}: {e}')
    print(f'  total: {got}')
    try: ws.close()
    except: pass
    print()

def reply_empty_text(ws, m):     ws.send('')
def reply_empty_bin(ws, m):      ws.send_binary(b'')
def reply_proto_ping(ws, m):     ws.send_binary(b'\x08\x00')     # f1=varint 0
def reply_settings(ws, m):       ws.send_binary(bytes.fromhex('1a0b08081207315f686f757273'))  # f3 settings

test('A: reply TEXT("")', reply_empty_text)
test('B: reply BIN(b"")', reply_empty_bin)
test('C: reply BIN(08 00)', reply_proto_ping)
test('D: reply BIN(settings f3)', reply_settings)