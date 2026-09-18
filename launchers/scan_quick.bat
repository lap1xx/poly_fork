@echo off
cd /d "%~dp0.."
python scan_2way.py --top 300 --show 20
pause
