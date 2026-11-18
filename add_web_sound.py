# add_web_sound.py
# Убирает beep() из polyarb_v4.py и патчит web_app.py (lifespan вместо on_event).
# Запускать ОДИН раз: python add_web_sound.py

import pathlib, re, sys

HERE = pathlib.Path(__file__).parent

# ---------- 1. Убираем beep из polyarb_v4.py ----------
P1 = HERE / "polyarb_v4.py"
if not P1.exists():
    print("[!] polyarb_v4.py не найден")
    sys.exit(1)

src = P1.read_text(encoding="utf-8")
pathlib.Path(str(P1) + ".bak").write_text(src, encoding="utf-8")

# Ищем блок:
#                     try:
#                         beep(live=is_live)
#                     except Exception:
#                         pass
pat = re.compile(
    r'(\n[ \t]+try:\n'
    r'[ \t]+beep\(live=is_live\)\n'
    r'[ \t]+except Exception:\n'
    r'[ \t]+pass\n)'
)

if "beep(live=is_live)" not in src:
    print("[=] beep() уже убран или отсутствует")
elif src.count("beep(live=is_live)") == 0:
    print("[=] ничего не нашли")
else:
    # комментируем сам вызов
    src = src.replace("beep(live=is_live)", "# beep(live=is_live)  # звук теперь в web_app")
    P1.write_text(src, encoding="utf-8")
    print("[+] beep() закомментирован в polyarb_v4.py")

# ---------- 2. web_app.py: on_event → lifespan ----------
P2 = HERE / "web_app.py"
if P2.exists():
    src2 = P2.read_text(encoding="utf-8")
    if "on_event(\"startup\")" in src2 and "lifespan" not in src2:
        pathlib.Path(str(P2) + ".bak").write_text(src2, encoding="utf-8")

        # Добавляем импорт
        src2 = src2.replace(
            "from fastapi import FastAPI, WebSocket, WebSocketDisconnect",
            "from contextlib import asynccontextmanager\n"
            "from fastapi import FastAPI, WebSocket, WebSocketDisconnect"
        )

        # Меняем создание app
        src2 = src2.replace(
            'app = FastAPI(title="Poly-ARB Web")',
            '@asynccontextmanager\n'
            'async def _lifespan(app):\n'
            '    asyncio.create_task(_broadcast_loop())\n'
            '    yield\n'
            '\n'
            'app = FastAPI(title="Poly-ARB Web", lifespan=_lifespan)'
        )

        # Удаляем старый on_event блок
        src2 = src2.replace(
            '@app.on_event("startup")\n'
            'async def _startup():\n'
            '    asyncio.create_task(_broadcast_loop())\n',
            ''
        )

        P2.write_text(src2, encoding="utf-8")
        print("[+] web_app.py: on_event → lifespan")
    else:
        print("[=] web_app.py уже патчен или структура другая")
else:
    print("[!] web_app.py не найден, пропускаем")

print()
print("Теперь замени файлы:")
print("    static/app.js")
print("    templates/index.html")
print("на новые версии (в этой пачке).")
print()
print("Готово.")
