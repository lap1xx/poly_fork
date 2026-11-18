# fix10.py - treat fn=7 with big payload as match push
import pathlib, ast

P = pathlib.Path(__file__).parent / "bb_bridge.py"
src = P.read_text(encoding="utf-8").replace("\r\n", "\n").replace("\r", "\n")
pathlib.Path(str(P) + ".bak").write_text(src, encoding="utf-8")
print(f"backup -> {P}.bak")

if "fn == 7 and len(r) > 500" in src:
    print("= already patched")
    raise SystemExit(0)

ANCHOR = "if fn == 29:\n                    self._handle_f29(bytes(r), label)\n"
if ANCHOR not in src:
    print("! anchor not found")
    raise SystemExit(1)

NEW = (
    "if fn == 29:\n"
    "                    self._handle_f29(bytes(r), label)\n"
    "                elif fn == 7 and len(r) > 500:\n"
    "                    print(f\"[BB {label}] f7-BIG -> _handle_f29 ({len(r)}B)\", flush=True)\n"
    "                    self._handle_f29(bytes(r), label)\n"
)

src = src.replace(ANCHOR, NEW, 1)
P.write_text(src, encoding="utf-8")
print("+ fn=7 big-payload handler added")

try:
    ast.parse(src)
    print("AST OK")
except SyntaxError as e:
    print(f"SYNTAX ERROR: {e}")
    raise SystemExit(1)