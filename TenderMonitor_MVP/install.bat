@echo off
chcp 65001 >nul
cd /d "%~dp0"
where py >nul 2>nul || (echo Установите Python 3.11/3.12 и отметьте Add Python to PATH & pause & exit /b 1)
if exist ".venv" (
  echo Удаляю старое виртуальное окружение .venv ...
  rmdir /s /q ".venv"
  if exist ".venv" (
    echo Не удалось удалить .venv: закройте программы, использующие Python из этой папки, и запустите install.bat снова.
    pause
    exit /b 1
  )
)
py -3 -m venv .venv
.venv\Scripts\python.exe -m pip install --upgrade pip
.venv\Scripts\python.exe -m pip install -r requirements.txt
echo.
echo Устанавливаю браузер Chromium для Playwright. Это может занять несколько минут...
.venv\Scripts\python.exe -m playwright install chromium
if errorlevel 1 (
  echo Не удалось скачать Chromium. Проверьте подключение к интернету и запустите install.bat снова.
  pause
  exit /b 1
)
.venv\Scripts\python.exe -m tendermonitor.cli init
echo Установка завершена. Запустите demo.bat
pause
