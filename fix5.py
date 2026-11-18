# fix5.py - robust reactive resub + hex dump of every distinct fn
import re, pathlib, ast

P = pathlib.Path(__file__).parent / "bb_bridge.py"
src = P.read_text(encoding="utf-8").replace("\r\n", "\n").replace("\r", "\n")

if "self._seen_fns" not in src:
    src = src.replace(
        "        self._f16_subs = set()",
        "        self._f16_subs = set()\n        self._seen_fns = set()",
        1,
    )
    print("+ _seen_fns in __init__")
else:
    print("= _seen_fns already")

pat = re.compile(r'(\n)([ \t]+)(if fn == 29:)')
m = pat.search(src)
if not m:
    print("! `if fn == 29:` NOT FOUND")
    raise SystemExit(1)

if "RESUB (any-frame)" in src:
    print("= reactive resub already there")
else:
    indent = m.group(2)
    block = (
        "\n{ind}# reactive resub on any frame (Chrome reloads kill the pipe)\n"
        "{ind}if fn not in self._seen_fns:\n"
        "{ind}    self._seen_fns.add(fn)\n"
        "{ind}    print(\"[HEX \" + label + \"] fn=\" + str(fn) + \" len=\" + str(len(r)) + \" hex=\" + bytes(r)[:100].hex(), flush=True)\n"
        "{ind}now = time.time()\n"
        "{ind}if now - self._last_sub_sent > 0.5:\n"
        "{ind}    try:\n"
        "{ind}        ws.send_binary(req_f6(sport_id, LIVE))\n"
        "{ind}        ws.send_binary(req_f6(sport_id, PREMATCH))\n"
        "{ind}        self._last_sub_sent = now\n"
        "{ind}        print(\"[BB \" + label + \"] RESUB (any-frame) after fn=\" + str(fn), flush=True)\n"
        "{ind}    except Exception as e:\n"
        "{ind}        print(\"[BB \" + label + \"] resub fail: \" + str(e), flush=True)\n"
    ).format(ind=indent)
    src = src[:m.start()] + block + src[m.start():]
    print("+ reactive resub inserted")

P.write_text(src, encoding="utf-8")
try:
    ast.parse(src)
    print("AST OK")
except SyntaxError as e:
    print(f"SYNTAX ERROR: {e}")
    raise SystemExit(1)