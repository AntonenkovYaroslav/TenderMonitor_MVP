@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Виртуальное окружение .venv не найдено или не собрано.
  echo Сначала запустите install.bat в папке проекта.
  pause
  exit /b 1
)
.venv\Scripts\python.exe -m tendermonitor.cli monitor-eis
echo.
pause
