# fix3.py - add periodic resubscribe to BBStream._connect_and_listen
import pathlib, re, ast

P = pathlib.Path(__file__).parent / "bb_bridge.py"
src = P.read_text(encoding="utf-8")

# --- 1. в классе BBStream: добавить флаг в __init__ ---
if "self._last_sub_sent = 0.0" not in src:
    src = src.replace(
        "        self._f16_subs = set()",
        "        self._f16_subs = set()\n"
        "        self._last_sub_sent = 0.0",
        1,
    )
    print("+ __init__ flag")

# --- 2. заменить блок subscribe в _connect_and_listen ---
OLD_BLOCK = """            try:
                ws.send_binary(req_settings())
                time.sleep(0.2)
                ws.send_binary(req_f4(domain, [LIVE]))
                time.sleep(0.4)
                ws.send_binary(req_f6(sport_id, LIVE))
                ws.send_binary(req_f6(sport_id, PREMATCH))
                print(f"[BB {label}] SUBSCRIBED (domain={domain}, sport={sport_id})", flush=True)
            except Exception as e:
                print(f"[BB {label}] subscribe send FAILED: {type(e).__name__}: {e}", flush=True)
"""

NEW_BLOCK = """            def _do_subscribe():
                try:
                    ws.send_binary(req_settings())
                    time.sleep(0.2)
                    ws.send_binary(req_f4(domain, [LIVE]))
                    time.sleep(0.4)
                    ws.send_binary(req_f6(sport_id, LIVE))
                    ws.send_binary(req_f6(sport_id, PREMATCH))
                    print(f"[BB {label}] SUBSCRIBED (domain={domain}, sport={sport_id})", flush=True)
                    return True
                except Exception as e:
                    print(f"[BB {label}] subscribe send FAILED: {type(e).__name__}: {e}", flush=True)
                    return False

            _do_subscribe()
            self._last_sub_sent = time.time()
            RESUB_EVERY = 20.0
"""

if OLD_BLOCK in src:
    src = src.replace(OLD_BLOCK, NEW_BLOCK, 1)
    print("+ subscribe block replaced")
elif "_do_subscribe" in src:
    print("= subscribe block already patched")
else:
    print("! subscribe block NOT FOUND — скинь текущий фрагмент _connect_and_listen")

# --- 3. в recv-цикле добавить resubscribe по таймауту ---
OLD_LOOP = """                except websocket.WebSocketTimeoutException:
                    continue
"""
NEW_LOOP = """                except websocket.WebSocketTimeoutException:
                    if time.time() - self._last_sub_sent > RESUB_EVERY:
                        print(f"[BB {label}] resubscribe (timer)", flush=True)
                        _do_subscribe()
                        self._last_sub_sent = time.time()
                    continue
"""
if OLD_LOOP in src:
    src = src.replace(OLD_LOOP, NEW_LOOP, 1)
    print("+ resub timer added")
elif "resubscribe (timer)" in src:
    print("= resub timer already there")
else:
    print("! timeout block NOT FOUND")

P.write_text(src, encoding="utf-8")

try:
    ast.parse(src)
    print("AST OK")
except SyntaxError as e:
    print(f"SYNTAX ERROR: {e}")
    raise SystemExit(1)