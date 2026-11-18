# fix9.py - drop subprotocols from bb_bridge create_connection
import pathlib, ast

P = pathlib.Path(__file__).parent / "bb_bridge.py"
src = P.read_text(encoding="utf-8").replace("\r\n", "\n").replace("\r", "\n")
pathlib.Path(str(P) + ".bak").write_text(src, encoding="utf-8")
print(f"backup -> {P}.bak")

before = src
src = src.replace(', subprotocols=["protobuf"]', '')
src = src.replace('subprotocols=["protobuf"], ', '')
src = src.replace('subprotocols=["protobuf"],', '')
if src == before:
    print("! subprotocol pattern NOT found — check manually")
    for i, line in enumerate(src.splitlines(), 1):
        if "subprotocols" in line:
            print(f"  {i}: {line}")
    raise SystemExit(1)

P.write_text(src, encoding="utf-8")
print("+ subprotocols stripped from bb_bridge")

for i, line in enumerate(src.splitlines(), 1):
    if "create_connection" in line or "subprotocols" in line:
        print(f"  {i}: {line}")

try:
    ast.parse(src)
    print("AST OK")
except SyntaxError as e:
    print(f"SYNTAX ERROR: {e}")
    raise SystemExit(1)