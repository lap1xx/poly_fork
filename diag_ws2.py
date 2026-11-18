# diag_ws2.py - what subprotocol did server actually negotiate?
import websocket, time

URL = 'wss://ru-ws2.sporthub.bet:443/api/tree_ws/v1'
HDR = {
    'User-Agent':'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36',
    'Referer':'https://betboom.ru/'
}

def test(name, subprotos):
    print(f"=== {name} (subprotocols={subprotos}) ===")
    try:
        ws = websocket.create_connection(
            URL, timeout=15,
            subprotocols=subprotos,
            origin='https://betboom.ru', header=HDR,
        )
    except Exception as e:
        print(f"  connect err: {type(e).__name__}: {e}")
        print()
        return
    print(f"  connected. negotiated subprotocol: {repr(ws.subprotocol)}")
    print(f"  ws headers back: {dict(ws.headers) if hasattr(ws,'headers') else '?'}")

    # 1. Binary
    try:
        ws.send_binary(b'\x08\x00')
        time.sleep(0.5)
        print(f"  after binary send: connected={ws.connected}")
    except Exception as e:
        print(f"  binary send err: {type(e).__name__}: {e}")

    ws.close()
    time.sleep(0.5)

    # 2. Text
    try:
        ws = websocket.create_connection(
            URL, timeout=15,
            subprotocols=subprotos,
            origin='https://betboom.ru', header=HDR,
        )
        ws.send("ping")
        time.sleep(0.5)
        print(f"  after text send: connected={ws.connected}")
        ws.close()
    except Exception as e:
        print(f"  text send err: {type(e).__name__}: {e}")
    print()

# T1: как сейчас (protobuf)
test("T1: protobuf", ["protobuf"])

# T2: без subprotocol
test("T2: no subprotocol", [])

# T3: graphql-ws (популярный)
test("T3: graphql-ws", ["graphql-ws"])

# T4: оба
test("T4: protobuf + graphql-ws", ["protobuf", "graphql-ws"])