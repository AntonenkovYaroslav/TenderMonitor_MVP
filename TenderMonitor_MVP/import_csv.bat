@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Виртуальное окружение .venv не найдено или не собрано.
  echo Сначала запустите install.bat в папке проекта.
  pause
  exit /b 1
)
set "FILE=%~1"
if "%FILE%"=="" set "FILE=data\incoming.csv"
if not exist "%FILE%" (
  echo Файл не найден: %FILE%
  echo Перетащите CSV-файл на import_csv.bat или запустите: import_csv.bat "C:\путь\к\файлу.csv"
  echo Без аргумента используется data\incoming.csv
  pause
  exit /b 1
)
.venv\Scripts\python.exe -m tendermonitor.cli import-csv "%FILE%"
pause
