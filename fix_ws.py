# fix_ws.py - drop subprotocol from local relay + Chrome hook
import pathlib, re

P = pathlib.Path(__file__).parent / "chrome_bridge.py"
src = P.read_text(encoding="utf-8").replace("\r\n", "\n").replace("\r", "\n")
backup = str(P) + ".bak"
pathlib.Path(backup).write_text(src, encoding="utf-8")
print(f"backup -> {backup}")

# 1. Убираем subprotocols=["protobuf"] из websockets.serve
before = src
src = src.replace(
    'subprotocols=["protobuf"],\n            ',
    '',
)
src = src.replace(
    ', subprotocols=["protobuf"]',
    '',
)
src = src.replace(
    'subprotocols=["protobuf"], ',
    '',
)
if src == before:
    # ищем как есть
    src = re.sub(r'subprotocols=\["protobuf"\],?\s*', '', src)
print("+ relay subprotocols stripped")

# 2. Убираем ['protobuf'] из Chrome hook (INIT_JS)
before = src
src = src.replace(
    "local = new Orig(LOCAL_URL, ['protobuf']);",
    "local = new Orig(LOCAL_URL);",
)
if src == before:
    print("! JS pattern not found - check INIT_JS manually")
else:
    print("+ Chrome hook subprotocol stripped")

P.write_text(src, encoding="utf-8")
print("written")
print("check:")
for i, line in enumerate(src.splitlines(), 1):
    if "subprotocols" in line:
        print(f"  {i}: {line}")
    if "new Orig(LOCAL_URL" in line:
        print(f"  {i}: {line}")