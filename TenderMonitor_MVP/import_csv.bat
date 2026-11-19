@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist data\incoming.csv (echo Создайте data\incoming.csv по инструкции README.md & pause & exit /b 1)
.venv\Scripts\python.exe -m tendermonitor.cli import-csv data\incoming.csv
pause
