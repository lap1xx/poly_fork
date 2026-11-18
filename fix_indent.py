# fix_indent.py - restore 4-space indent for _run_loop and _connect_and_listen
import pathlib, shutil

P = pathlib.Path(__file__).parent / "bb_bridge.py"
shutil.copy(P, str(P) + ".bak")

lines = P.read_text(encoding="utf-8").splitlines(keepends=True)

fixed = 0
for i in range(len(lines)):
    s = lines[i].lstrip()
    if s.startswith("def _run_loop(self,") or s.startswith("def _connect_and_listen(self,"):
        lines[i] = "    " + lines[i]
        fixed += 1

# после этого нужно проиндентифицировать тела этих двух функций до следующей
# уже-индентифицированной строки
out = []
in_block = False
for i, ln in enumerate(lines):
    stripped = ln.lstrip()
    if stripped.startswith("def _run_loop(self,") or stripped.startswith("def _connect_and_listen(self,"):
        in_block = True
        out.append(ln)
        continue
    if in_block:
        if ln.startswith("    "):
            in_block = False
            out.append(ln)
            continue
        if ln.strip() == "":
            out.append(ln)
            continue
        out.append("    " + ln)
        continue
    out.append(ln)

P.write_text("".join(out), encoding="utf-8")
print(f"fixed headers: {fixed}")
print("backup -> bb_bridge.py.bak")