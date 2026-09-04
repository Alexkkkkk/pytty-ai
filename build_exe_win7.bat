@echo off
chcp 65001 >nul
title Сборка PuTTY-AI (Windows 7)
cd /d "%~dp0"

python --version >nul 2>&1
if errorlevel 1 (
    echo [ОШИБКА] Python не найден. Установите Python 3.8.10:
    echo https://www.python.org/downloads/release/python-3810/
    echo ВАЖНО: галочка "Add Python 3.8 to PATH"!
    pause
    exit /b 1
)

echo [1/3] Установка зависимостей для сборки...
python -m pip install --upgrade pip >nul
python -m pip install -r requirements-build-win7.txt
if errorlevel 1 ( echo [ОШИБКА] pip install & pause & exit /b 1 )

echo [2/3] Сборка .exe (2-5 минут)...
python -m PyInstaller --onefile --noconsole --name "PuTTY-AI" ^
    --add-data "u_boot_errors_kb.md;." ^
    --add-data "learned_cases.md;." ^
    --add-data "skills.json;." ^
    --add-data "learned_rules.json;." ^
    --add-data "user_patches.py;." ^
    --icon app.ico ^
    --hidden-import serial --hidden-import serial.tools.list_ports ^
    --hidden-import paramiko ^
    putty_ai_win7.py
if errorlevel 1 ( echo [ОШИБКА] Сборка не удалась & pause & exit /b 1 )

echo [3/3] Готово!
echo ============================================
echo   Файл: dist\PuTTY-AI.exe
echo ============================================
pause
