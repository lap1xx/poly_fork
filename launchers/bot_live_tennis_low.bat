@echo off
cd /d "%~dp0.."
python bot_live.py --sport TENNIS --only-low
pause
