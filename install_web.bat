@echo off
setlocal
cd /d "%~dp0"

echo === Poly-ARB local dashboard setup ===

set "PY=python"
where py >nul 2>&1
if not errorlevel 1 set "PY=py -3"

if not exist ".venv\Scripts\python.exe" (
    echo Creating isolated .venv...
    %PY% -m venv .venv
    if errorlevel 1 (
        echo [!] Could not create .venv. Install Python 3 and retry.
        pause
        exit /b 1
    )
)

.venv\Scripts\python.exe -m pip install --upgrade pip
if errorlevel 1 (
    echo [!] Failed to update pip.
    pause
    exit /b 1
)

.venv\Scripts\python.exe -m pip install -r requirements.txt
if errorlevel 1 (
    echo [!] Dependency installation failed.
    pause
    exit /b 1
)

.venv\Scripts\python.exe -c "import fastapi, requests, uvicorn, websocket; print('Dependencies OK')"
if errorlevel 1 (
    echo [!] Dependency verification failed.
    pause
    exit /b 1
)

echo.
echo Setup complete. Double-click bot_live.bat to start the scanner and open the dashboard.
echo Use start_all.bat only when you want to open the dashboard without starting the scanner.
echo Legacy patch scripts and browser bridges were not run.
pause
