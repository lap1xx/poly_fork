@echo off
cd /d "%~dp0.."
python scan_live.py --sport DOTA2 --only-low
pause
