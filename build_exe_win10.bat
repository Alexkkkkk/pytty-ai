@echo off
chcp 65001 >nul
title Сборка PuTTY-AI (Windows 10/11)
cd /d "%~dp0"

python --version >nul 2>&1
if errorlevel 1 (
    echo [ОШИБКА] Python не найден. Установите Python 3.11+ с https://www.python.org
    echo ВАЖНО: галочка "Add python.exe to PATH"!
    pause
    exit /b 1
)

echo [1/3] Установка зависимостей для сборки...
python -m pip install --upgrade pip >nul
python -m pip install -r requirements-build.txt
if errorlevel 1 ( echo [ОШИБКА] pip install & pause & exit /b 1 )

echo [2/3] Сборка .exe (2-5 минут)...
python -m PyInstaller --clean --noconfirm PuTTY-AI.spec
if errorlevel 1 ( echo [ОШИБКА] Сборка не удалась & pause & exit /b 1 )

echo [3/3] Готово!
echo ============================================
echo   Файл: dist\PuTTY-AI.exe
echo ============================================
echo К exe приложены база знаний и иконка.
pause
