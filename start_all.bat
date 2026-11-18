@echo off
setlocal
cd /d "%~dp0"

echo === POLY-ARB local dashboard ===

echo This launcher starts only the local dashboard.
echo It does not kill existing processes, start a bookmaker scanner,
echo open a remote-debugging browser, or run the legacy Chrome bridge.

echo.
if not exist ".venv\Scripts\python.exe" (
    echo [!] Virtual environment not found. Run install_web.bat first.
    pause
    exit /b 1
)

start "Poly-ARB dashboard" "%~dp0.venv\Scripts\python.exe" "%~dp0web_app.py"
timeout /t 2 /nobreak >nul
start "" "http://127.0.0.1:8080"

echo.
echo Dashboard: http://127.0.0.1:8080
echo Existing arbs_live.json snapshots may be displayed; no scanner was started.
echo.
