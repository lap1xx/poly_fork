@echo off
cd /d "%~dp0.."
python scan_live.py --sport CS2 --only-low
pause
