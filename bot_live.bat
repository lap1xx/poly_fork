@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo [!] Virtual environment not found. Run install_web.bat first.
    pause
    exit /b 1
)
".venv\Scripts\python.exe" -B bot_live.py --no-bb --min-roi 3.0 %*
set "EXIT_CODE=%ERRORLEVEL%"
pause
exit /b %EXIT_CODE%
