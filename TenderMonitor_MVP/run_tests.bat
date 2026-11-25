@echo off
rem Полный прогон проверок Tender Monitor (7 наборов, 505 проверок).
rem Требует .venv (install.bat). Подробности: tests\README.md
chcp 65001 >nul
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo [ОШИБКА] Не найдено виртуальное окружение .venv. Сначала запустите install.bat
    exit /b 1
)
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1
set FAILED=0
for %%T in (test_profile test_build test_iter2 test_iter3 test_web test_pagination_render test_dedup) do (
    echo === %%T ===
    ".venv\Scripts\python.exe" "tests\%%T.py"
    if errorlevel 1 set FAILED=1
)
if "%FAILED%"=="1" (
    echo [ПРОВАЛ] Есть упавшие проверки — смотрите вывод выше.
    exit /b 1
)
echo [ОК] Все 7 наборов проверок прошли.
endlocal