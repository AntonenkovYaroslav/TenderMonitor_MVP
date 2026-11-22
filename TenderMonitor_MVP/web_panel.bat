@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Виртуальное окружение .venv не найдено или не собрано.
  echo Сначала запустите install.bat в папке проекта.
  pause
  exit /b 1
)
echo Веб-панель Tender Monitor: http://127.0.0.1:8000/
echo Логин по умолчанию: admin / admin (смените пароль при первом входе).
echo Остановка сервера: Ctrl+C в этом окне.
.venv\Scripts\python.exe -m tendermonitor.cli web --host 127.0.0.1 --port 8000
echo.
pause
