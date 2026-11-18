@echo off
setlocal
cd /d "%~dp0"

echo === POLY-ARB launcher ===

set "CHROME=%PROGRAMFILES%\Google\Chrome\Application\chrome.exe"
if not exist "%CHROME%" set "CHROME=%PROGRAMFILES(X86)%\Google\Chrome\Application\chrome.exe"
if not exist "%CHROME%" set "CHROME=%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"
if not exist "%CHROME%" goto no_chrome

echo Chrome: %CHROME%

taskkill /F /IM chrome.exe >nul 2>&1
taskkill /F /IM python.exe >nul 2>&1
timeout /t 2 /nobreak >nul

echo Starting Chrome with 2 tabs...
start "" "%CHROME%" --remote-debugging-port=9222 --user-data-dir="%LOCALAPPDATA%\poly-arb-chrome" --no-first-run --no-default-browser-check --disable-features=BlockInsecurePrivateNetworkRequests,PrivateNetworkAccessSendPreflights,PrivateNetworkAccessRespectPreflightResults,LocalNetworkAccessChecks https://betboom.ru/sport/live/esports https://betboom.ru/sport/live/tennis https://betboom.ru/sport/esports https://betboom.ru/sport/tennis

echo Waiting 12s for page load...
timeout /t 30 /nobreak >nul

echo Starting chrome_bridge.py...
start "poly-arb-bridge" cmd /k python chrome_bridge.py

echo Waiting 10s for tree_ws hook...
timeout /t 10 /nobreak >nul

echo Starting bot_live.py...
start "poly-arb-bot" cmd /k python -B bot_live.py

echo.
echo === All started ===
echo Dashboard: http://localhost:8080
echo Two windows opened: poly-arb-bridge, poly-arb-bot
echo Keep them open. This window can be closed.
echo.
pause
goto :eof

:no_chrome
echo Chrome not found. Edit CHROME variable manually.
pause
exit /b 1