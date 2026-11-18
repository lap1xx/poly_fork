"""
patch_bridge.py - вставляет фильтр f8_sport в bb_bridge._store_f29_result.
Делает .bak-копию bb_bridge.py. Безопасно запускать повторно.
"""
import shutil
import sys

PATH = "bb_bridge.py"

shutil.copy(PATH, PATH + ".bak")
print(f"[patch] backup сохранён: {PATH}.bak")

with open(PATH, encoding="utf-8") as f:
    lines = f.readlines()

# Найти def _store_f29_result
idx_def = None
for i, ln in enumerate(lines):
    if "def _store_f29_result(self" in ln:
        idx_def = i
        break

if idx_def is None:
    print("[patch] ОШИБКА: не нашёл 'def _store_f29_result'")
    sys.exit(1)

print(f"[patch] def _store_f29_result на строке {idx_def + 1}")

# Найти внутри метода строку 'if match and "match_id" in match:'
idx_check = None
for i in range(idx_def + 1, min(idx_def + 20, len(lines))):
    if 'if match and "match_id" in match:' in lines[i]:
        idx_check = i
        break

if idx_check is None:
    print("[patch] ОШИБКА: не нашёл 'if match and \"match_id\" in match:' внутри метода")
    print("      Покажи строки", idx_def + 1, "-", idx_def + 15)
    sys.exit(1)

print(f"[patch] if match_id на строке {idx_check + 1}")

# Проверить есть ли уже фильтр
next_few = "".join(lines[idx_check + 1:idx_check + 3])
if "f8_sport" in next_few:
    print("[patch] фильтр УЖЕ на месте — пропускаю вставку фильтра")
else:
    indent = " " * (len(lines[idx_check]) - len(lines[idx_check].lstrip()))
    filter_lines = [
        f"{indent}    if match.get(\"f8_sport\") not in self._KEEP_F8:\n",
        f"{indent}        return\n",
    ]
    for off, nl in enumerate(filter_lines):
        lines.insert(idx_check + 1 + off, nl)
    print(f"[patch] вставлен фильтр f8_sport (отступ {len(indent)+4})")

# Проверить _KEEP_F8
if any("_KEEP_F8 = {1, 4}" in ln for ln in lines):
    print("[patch] _KEEP_F8 уже есть")
else:
    indent = " " * (len(lines[idx_def]) - len(lines[idx_def].lstrip()))
    lines.insert(idx_def, f"{indent}_KEEP_F8 = {{1, 4}}\n\n")
    print(f"[patch] _KEEP_F8 добавлен перед def (строка {idx_def + 1})")

with open(PATH, "w", encoding="utf-8") as f:
    f.writelines(lines)

print("[patch] Готово. Проверь: python -c \"import bb_bridge; print('ok')\"")
