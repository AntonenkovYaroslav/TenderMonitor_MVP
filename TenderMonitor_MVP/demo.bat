@echo off
chcp 65001 >nul
cd /d "%~dp0"
.venv\Scripts\python.exe -m tendermonitor.cli import-csv data\sample_tenders.csv
.venv\Scripts\python.exe -m tendermonitor.cli export
pause
