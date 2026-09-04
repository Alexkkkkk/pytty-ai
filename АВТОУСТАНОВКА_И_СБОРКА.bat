@echo off
chcp 65001 >nul
title PuTTY-AI: всё на автомате
cd /d "%~dp0"
echo ============================================
echo   PuTTY-AI — автоустановка и сборка
echo   (Python + зависимости + exe на Рабочем столе)
echo ============================================
echo.

REM --- 1. Python: проверка, при необходимости автоустановка через winget ---
python --version >nul 2>&1
if errorlevel 1 (
    echo Python не найден — ставлю автоматически...
    where winget >nul 2>&1
    if errorlevel 1 (
        echo [ОШИБКА] Нет ни Python, ни winget.
        echo Установите вручную: https://www.python.org/downloads/
        echo (галочка "Add python.exe to PATH" при установке!)
        pause & exit /b 1
    )
    winget install -e --id Python.Python.3.11 --accept-source-agreements --accept-package-agreements
    :: подхватываем свежий PATH без перезапуска окна
    for /f "tokens=2*" %%a in ('reg query "HKCU\Environment" /v Path 2^>nul ^| findstr "Path"') do set "PATH=%%b"
    set "PATH=%PATH%;%LOCALAPPDATA%\Programs\Python\Python311\;%LOCALAPPDATA%\Programs\Python\Python311\Scripts\"
)
python --version >nul 2>&1
if errorlevel 1 (
    echo [ВНИМАНИЕ] Python установлен, но не виден в PATH.
    echo Закройте это окно и запустите файл ещё раз.
    pause & exit /b 1
)
python --version

echo.
echo [1/3] Установка зависимостей...
python -m pip install --upgrade pip >nul
python -m pip install -r requirements-build.txt
if errorlevel 1 ( echo [ОШИБКА] pip install & pause & exit /b 1 )

echo.
echo [2/3] Сборка PuTTY-AI.exe (2-5 минут, окно можно свернуть)...
python -m PyInstaller --clean --noconfirm PuTTY-AI.spec
if errorlevel 1 ( echo [ОШИБКА] сборка & pause & exit /b 1 )

echo.
echo [3/3] Копирую готовый exe на Рабочий стол...
copy /y "dist\PuTTY-AI.exe" "%USERPROFILE%\Desktop\PuTTY-AI.exe" >nul

echo.
echo ============================================
echo   ГОТОВО!  PuTTY-AI.exe — на Рабочем столе
echo ============================================
set /p RUNIT="Запустить программу сейчас? (y/n): "
if /i "%RUNIT%"=="y" start "" "%USERPROFILE%\Desktop\PuTTY-AI.exe"
pause
