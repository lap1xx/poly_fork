@echo off
cd /d "%~dp0.."
python scan_live.py --only-low
pause
