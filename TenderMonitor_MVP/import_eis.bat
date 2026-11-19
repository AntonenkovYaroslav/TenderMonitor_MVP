@echo off
chcp 65001 >nul
cd /d "%~dp0"
if "%~1"=="" (
  echo Перетащите ZIP или XML файл ЕИС на import_eis.bat
  echo Либо запустите: import_eis.bat "C:\путь\к\выгрузке.zip"
  pause
  exit /b 1
)
.venv\Scripts\python.exe -m tendermonitor.cli inspect-eis "%~1"
echo.
echo Проверка завершена. Для импорта используйте команду из инструкции.
pause
