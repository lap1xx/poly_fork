# fix6.py - resub on ANY frame (robust, no pattern coupling)
import pathlib, ast

P = pathlib.Path(__file__).parent / "bb_bridge.py"
src = P.read_text(encoding="utf-8").replace("\r\n", "\n").replace("\r", "\n")

if "self._seen_fns" not in src:
    src = src.replace(
        "        self._f16_subs = set()",
        "        self._f16_subs = set()\n        self._seen_fns = set()",
        1,
    )
    print("+ _seen_fns in __init__")

ANCHOR = '            print(f"[RAW {label}] fn={fn} len={len(r)}", flush=True)\n'

if "RESUB (any-frame)" in src:
    print("= already patched")
elif ANCHOR not in src:
    print("! anchor not found")
    raise SystemExit(1)
else:
    block = ANCHOR + (
        '            if fn not in self._seen_fns:\n'
        '                self._seen_fns.add(fn)\n'
        '                print(f"[HEX {label}] fn={fn} len={len(r)} hex={bytes(r)[:120].hex()}", flush=True)\n'
        '            _now = time.time()\n'
        '            if _now - self._last_sub_sent > 0.5:\n'
        '                try:\n'
        '                    ws.send_binary(req_f6(sport_id, LIVE))\n'
        '                    ws.send_binary(req_f6(sport_id, PREMATCH))\n'
        '                    self._last_sub_sent = _now\n'
        '                    print(f"[BB {label}] RESUB (any-frame) after fn={fn}", flush=True)\n'
        '                except Exception as _e:\n'
        '                    print(f"[BB {label}] resub fail: {_e}", flush=True)\n'
    )
    src = src.replace(ANCHOR, block, 1)
    print("+ any-frame resub inserted")

P.write_text(src, encoding="utf-8")
try:
    ast.parse(src)
    print("AST OK")
except SyntaxError as e:
    print(f"SYNTAX ERROR: {e}")
    raise SystemExit(1)