# fix13.py - silence resubscribe + SUBSCRIBED spam
import pathlib, ast

P = pathlib.Path(__file__).parent / "bb_bridge.py"
src = P.read_text(encoding="utf-8").replace("\r\n", "\n").replace("\r", "\n")
pathlib.Path(str(P) + ".bak").write_text(src, encoding="utf-8")
print(f"backup -> {P}.bak")

targets = [
    # "resubscribe (timer)"
    '                        print(f"[BB {label}] resubscribe (timer)", flush=True)\n',
    # "SUBSCRIBED ... f6_hex="
    '                    print(f"[BB {label}] SUBSCRIBED (domain={domain}, sport={sport_id}) f6_hex={p6.hex()}", flush=True)\n',
    # old form without hex (на случай если fix4 не отработал)
    '                    print(f"[BB {label}] SUBSCRIBED (domain={domain}, sport={sport_id})", flush=True)\n',
    # hex-строки от fix7/fix5 (если применялись)
    '                print(f"[HEX {label}] fn={fn} len={len(r)} hex={bytes(r)[:120].hex()}", flush=True)\n',
]

n = 0
for old in targets:
    if old in src:
        indent = len(old) - len(old.lstrip())
        new = " " * indent + "# " + old.lstrip()
        src = src.replace(old, new, 1)
        n += 1
        print(f"+ silenced: {old.strip()[:70]}...")

if n == 0:
    print("! nothing to silence — уже закомментировано")
    raise SystemExit(0)

P.write_text(src, encoding="utf-8")

try:
    ast.parse(src)
    print(f"AST OK ({n} lines silenced)")
except SyntaxError as e:
    print(f"SYNTAX ERROR: {e}")
    raise SystemExit(1)