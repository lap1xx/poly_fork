# fix11.py
import pathlib, ast

P = pathlib.Path(__file__).parent / "bb_bridge.py"
src = P.read_text(encoding="utf-8").replace("\r\n", "\n").replace("\r", "\n")
pathlib.Path(str(P) + ".bak").write_text(src, encoding="utf-8")

if "big fn=" in src:
    print("= already patched")
    raise SystemExit(0)

ANCHOR = '                print(f"[RAW {label}] fn={fn} len={len(r)}", flush=True)\n'
if ANCHOR not in src:
    print("! anchor not found")
    raise SystemExit(1)

BLOCK = ANCHOR + (
    '                if len(r) > 500 and fn != 5:\n'
    '                    print(f"[BB {label}] big fn={fn} -> _handle_f30 ({len(r)}B)", flush=True)\n'
    '                    try:\n'
    '                        self._handle_f30(bytes(r), label)\n'
    '                    except Exception as _e:\n'
    '                        print(f"[BB {label}] big err: {_e}", flush=True)\n'
    '                    continue\n'
)

src = src.replace(ANCHOR, BLOCK, 1)
P.write_text(src, encoding="utf-8")
print("+ done")

try:
    ast.parse(src)
    print("AST OK")
except SyntaxError as e:
    print(f"SYNTAX ERROR: {e}")
    raise SystemExit(1)