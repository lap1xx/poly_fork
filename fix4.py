# fix4.py - resend f6 on every fn=5 (Chrome alive signal)
import pathlib, ast

P = pathlib.Path(__file__).parent / "bb_bridge.py"
src = P.read_text(encoding="utf-8")

# --- 1. В recv-цикле: на fn=5 переотправляем f6 ---
OLD = """                if fn == 29:
                    self._handle_f29(bytes(r), label)"""

NEW = """                if fn == 5:
                    # tree arrived - Chrome hook is alive, resend subscribe
                    now = time.time()
                    if now - self._last_sub_sent > 1.0:
                        try:
                            ws.send_binary(req_f6(sport_id, LIVE))
                            ws.send_binary(req_f6(sport_id, PREMATCH))
                            self._last_sub_sent = now
                            print(f"[BB {label}] RESUB on fn=5", flush=True)
                        except Exception as e:
                            print(f"[BB {label}] resub fn=5 fail: {e}", flush=True)
                elif fn == 29:
                    self._handle_f29(bytes(r), label)"""

if OLD in src:
    src = src.replace(OLD, NEW, 1)
    print("+ fn=5 resub added")
elif "RESUB on fn=5" in src:
    print("= already patched")
else:
    print("! recv-loop pattern NOT FOUND")

# --- 2. Уменьшаем таймер resub с 20 до 8 сек ---
src2 = src.replace("RESUB_EVERY = 20.0", "RESUB_EVERY = 8.0")
if src2 != src:
    src = src2
    print("+ RESUB_EVERY -> 8")

# --- 3. Печатаем hex f6 при первом subscribe ---
OLD2 = """                    ws.send_binary(req_f6(sport_id, LIVE))
                    ws.send_binary(req_f6(sport_id, PREMATCH))
                    print(f"[BB {label}] SUBSCRIBED (domain={domain}, sport={sport_id})", flush=True)"""
NEW2 = """                    p6 = req_f6(sport_id, LIVE)
                    ws.send_binary(p6)
                    ws.send_binary(req_f6(sport_id, PREMATCH))
                    print(f"[BB {label}] SUBSCRIBED (domain={domain}, sport={sport_id}) f6_hex={p6.hex()}", flush=True)"""

if OLD2 in src:
    src = src.replace(OLD2, NEW2, 1)
    print("+ f6 hex log")
elif "f6_hex=" in src:
    print("= hex log already")

P.write_text(src, encoding="utf-8")

try:
    ast.parse(src)
    print("AST OK")
except SyntaxError as e:
    print(f"SYNTAX ERROR: {e}")
    raise SystemExit(1)