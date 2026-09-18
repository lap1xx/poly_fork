@echo off
cd /d "%~dp0.."
python bot_live.py --sport DOTA2 --only-low
pause
