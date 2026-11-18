import shutil, sys, re

PATH = "chrome_bridge.py"
TARGET = "https://betboom.ru/esport"

shutil.copy(PATH, PATH + ".bak")
print(f"[patch] backup: {PATH}.bak")

with open(PATH, encoding="utf-8") as f:
    src = f.read()

new = f'BB_URL = "{TARGET}"'
old = 'BB_URL = "https://betboom.ru/sport/live"'

if old in src:
    src = src.replace(old, new)
    print("[patch] BB_URL: sport/live -> esport")
elif new in src:
    print("[patch] BB_URL уже esport")
else:
    m = re.search(r'^BB_URL\s*=\s*".*"', src, re.M)
    if not m:
        print("[patch] ОШИБКА: BB_URL не найден")
        sys.exit(1)
    src = src[:m.start()] + new + src[m.end():]
    print(f"[patch] BB_URL переписан: {m.group(0)} -> {new}")

with open(PATH, "w", encoding="utf-8") as f:
    f.write(src)

print("[patch] Готово. Перезапусти chrome_bridge.py")