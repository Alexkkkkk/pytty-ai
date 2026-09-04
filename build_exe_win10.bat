@echo off
chcp 65001 >nul
title Сборка PuTTY-AI в exe
cd /d "%~dp0"

echo ============================================
echo   PuTTY-AI - сборка .exe
echo ============================================
echo.

REM --- проверяем Python ---
python --version >nul 2>&1
if errorlevel 1 (
    echo [ОШИБКА] Python не найден!
    echo.
    echo Установите Python 3.11+ с https://www.python.org/downloads/
    echo ВАЖНО: при установке поставьте галочку "Add python.exe to PATH"
    echo.
    pause
    exit /b 1
)

echo [1/3] Установка зависимостей...
python -m pip install --upgrade pip >nul
python -m pip install PyQt6 paramiko pyinstaller
if errorlevel 1 (
    echo [ОШИБКА] Не удалось установить пакеты. Проверьте интернет.
    pause
    exit /b 1
)

echo [2/3] Сборка .exe...
python -m PyInstaller --onefile --noconsole --name "PuTTY-AI" putty_ai_win10.py
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
echo Можно переносить на любой Windows 10/11.
echo.
pause
