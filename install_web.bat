@echo off
REM Установка веб-интерфейса Poly-ARB. Запускать ОДИН раз.
REM Затем просто запускай bot_live.py как обычно.

echo === [1/5] Установка зависимостей ===
pip install fastapi uvicorn[standard] jinja2
if errorlevel 1 (
    echo [!] Ошибка установки зависимостей
    pause
    exit /b 1
)

echo.
echo === [2/5] Создание структуры ===
if not exist templates mkdir templates
if not exist static mkdir static
echo     templates\ static\ OK

echo.
echo === [3/5] Патч polyarb_v4.py ===
python polyarb_v4_patch.py
if errorlevel 1 (
    echo [!] Ошибка патча polyarb_v4.py
    pause
    exit /b 1
)

echo.
echo === [4/5] Патч bot_live.py ===
python bot_live_patch.py
if errorlevel 1 (
    echo [!] Ошибка патча bot_live.py
    pause
    exit /b 1
)

echo.
echo === [5/5] Проверка ===
python -c "import fastapi, uvicorn; print('    fastapi', fastapi.__version__)"

echo.
echo === ГОТОВО ===
echo Теперь запускай как обычно:
echo     python chrome_bridge.py    (окно 1)
echo     python -B bot_live.py      (окно 2)
echo И открывай в браузере:
echo     http://localhost:8080
pause
