@echo off
setlocal
echo Poly-ARB supported Windows launchers:
echo   ..\install_web.bat - create the virtual environment and install dependencies
echo   bot_live.bat - start the scanner and open the dashboard (1win active by default)
echo   ..\start_all.bat - start the dashboard only; it does not start the scanner
echo.
echo From this folder, run bot_live.bat to delegate to the repository-root launcher.
echo Optional arguments: --min-roi PERCENT, --bank USDT, --no-web
echo Change minimum ROI and enabled bookmaker feeds live in the dashboard; no scanner restart is needed.
echo Sport-specific and low-only shortcuts are not supported by the current scanner.
pause
