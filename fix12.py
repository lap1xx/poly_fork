# fix12.py - silence RAW spam
import pathlib, ast

P = pathlib.Path(__file__).parent / "bb_bridge.py"
src = P.read_text(encoding="utf-8").replace("\r\n", "\n").replace("\r", "\n")
pathlib.Path(str(P) + ".bak").write_text(src, encoding="utf-8")
print(f"backup -> {P}.bak")

replacements = [
    (
        '                print(f"[RAW {label}] fn={fn} len={len(r)}", flush=True)\n',
        '                # print(f"[RAW {label}] fn={fn} len={len(r)}", flush=True)\n',
    ),
    (
        '                    print(f"[BB {label}] big fn={fn} -> _handle_f30 ({len(r)}B)", flush=True)\n',
        '                    # print(f"[BB {label}] big fn={fn} -> _handle_f30 ({len(r)}B)", flush=True)\n',
    ),
    (
        '                print(f"[BB {label}] RESUB (any-frame) after fn={fn}", flush=True)\n',
        '                # print(f"[BB {label}] RESUB (any-frame) after fn={fn}", flush=True)\n',
    ),
]

n = 0
for old, new in replacements:
    if old in src:
        src = src.replace(old, new, 1)
        n += 1
        print(f"+ commented: {old.strip()[:60]}...")

if n == 0:
    print("! nothing replaced — уже закомментировано или шаблоны изменились")
    raise SystemExit(0)

P.write_text(src, encoding="utf-8")

try:
    ast.parse(src)
    print(f"AST OK ({n} lines silenced)")
except SyntaxError as e:
    print(f"SYNTAX ERROR: {e}")
    raise SystemExit(1)