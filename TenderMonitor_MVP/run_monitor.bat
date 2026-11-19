@echo off
chcp 65001 >nul
cd /d "%~dp0"

if not exist ".venv\Scripts\activate.bat" (
  echo Python virtual environment .venv was not found.
  echo Run install.bat in the project folder first.
  pause
  exit /b 1
)

call ".venv\Scripts\activate.bat"
python -m tendermonitor.cli monitor-eis
echo.
pause
