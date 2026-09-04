@echo off
chcp 65001 >nul
title Сборка PuTTY-AI в exe (Win7)
cd /d "%~dp0"

echo ============================================
echo   PuTTY-AI Win7 - сборка .exe
echo ============================================
echo.

REM --- проверяем Python ---
python --version >nul 2>&1
if errorlevel 1 (
    echo [ОШИБКА] Python не найден!
    echo.
    echo Установите Python 3.8.10:
    echo https://www.python.org/downloads/release/python-3810/
    echo Файл: python-3.8.10-amd64.exe
    echo ВАЖНО: при установке поставьте галочку "Add Python 3.8 to PATH"
    echo.
    pause
    exit /b 1
)

echo [1/3] Установка зависимостей...
python -m pip install --upgrade pip >nul
python -m pip install PyQt5==5.15.11 paramiko==3.4.1 pyinstaller
if errorlevel 1 (
    echo [ОШИБКА] Не удалось установить пакеты. Проверьте интернет.
    pause
    exit /b 1
)

echo [2/3] Сборка .exe...
python -m PyInstaller --onefile --noconsole --name "PuTTY-AI" putty_ai_win7.py
if errorlevel 1 (
    echo [ОШИБКА] Сборка не удалась.
    pause
    exit /b 1
)

echo [3/3] Готово!
echo.
echo ============================================
echo   Файл:  dist\PuTTY-AI.exe
echo ============================================
echo.
echo Можно переносить на любой Windows 7.
echo.
pause
