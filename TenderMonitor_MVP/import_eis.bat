@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Виртуальное окружение .venv не найдено или не собрано.
  echo Сначала запустите install.bat в папке проекта.
  pause
  exit /b 1
)
if "%~1"=="" (
  echo Перетащите ZIP или XML файл ЕИС на import_eis.bat
  echo Либо запустите: import_eis.bat "C:\путь\к\выгрузке.zip"
  pause
  exit /b 1
)
if not exist "%~1" (
  echo Файл не найден: %~1
  pause
  exit /b 1
)
echo Импортирую "%~1" ...
.venv\Scripts\python.exe -m tendermonitor.cli import-eis "%~1"
pause
