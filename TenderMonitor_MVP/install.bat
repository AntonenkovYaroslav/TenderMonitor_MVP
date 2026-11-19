@echo off
chcp 65001 >nul
cd /d "%~dp0"
where py >nul 2>nul || (echo Установите Python 3.11/3.12 и отметьте Add Python to PATH & pause & exit /b 1)
py -3 -m venv .venv
.venv\Scripts\python.exe -m pip install --upgrade pip
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe -m tendermonitor.cli init
echo Установка завершена. Запустите demo.bat
pause
