@echo off
cd /d "%~dp0.."
python scan_live.py --sport TENNIS --only-low
pause
