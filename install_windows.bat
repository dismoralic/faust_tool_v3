@echo off
setlocal EnableExtensions DisableDelayedExpansion
chcp 65001 >nul
cd /d "%~dp0"
title Faust Tool v3 - установка

echo.
echo ========================================
echo       Faust Tool v3 - установка
echo ========================================
echo.

if not exist "requirements-core.txt" (
    echo Ошибка: установщик должен находиться в папке Faust Tool.
    echo Скачайте весь репозиторий через Code ^> Download ZIP, распакуйте его и запустите этот файл снова.
    pause
    exit /b 1
)

call :find_python
if defined PYTHON_BIN goto python_ready

where winget >nul 2>&1
if errorlevel 1 (
    echo Python 3.10 или новее не найден, а winget недоступен.
    echo Установите Python с https://www.python.org/downloads/windows/
    echo При установке включите Add Python to PATH, затем запустите этот файл снова.
    pause
    exit /b 1
)

echo Python не найден. Устанавливаю Python 3.12 через winget...
winget install --exact --id Python.Python.3.12 --source winget --accept-package-agreements --accept-source-agreements
if errorlevel 1 (
    echo Не удалось установить Python автоматически.
    pause
    exit /b 1
)

call :find_python
if not defined PYTHON_BIN (
    if exist "%LocalAppData%\Programs\Python\Python312\python.exe" (
        set "PYTHON_BIN=%LocalAppData%\Programs\Python\Python312\python.exe"
        set "PYTHON_ARGS="
    )
)
if not defined PYTHON_BIN (
    echo Python установлен, но текущая консоль ещё не видит его.
    echo Закройте это окно и запустите install_windows.bat снова.
    pause
    exit /b 1
)

:python_ready
echo Создаю отдельное окружение Python...
"%PYTHON_BIN%" %PYTHON_ARGS% -m venv ".venv"
if errorlevel 1 goto install_failed

echo Обновляю установщик пакетов...
".venv\Scripts\python.exe" -m pip install --upgrade pip setuptools wheel
if errorlevel 1 goto install_failed

echo Устанавливаю основные зависимости...
".venv\Scripts\python.exe" -m pip install -r "requirements-core.txt"
if errorlevel 1 goto install_failed

echo Устанавливаю ускорение cryptg...
".venv\Scripts\python.exe" -m pip install "cryptg>=0.5,<1"
if errorlevel 1 echo cryptg не установился. Faust Tool продолжит работать без ускорения.

echo.
echo Настройка Telegram API и Ollama...
".venv\Scripts\python.exe" "setup_config.py"
if errorlevel 1 goto install_failed

echo.
echo Установка завершена.
choice /C YN /N /M "Запустить Faust Tool сейчас? [Y/N] "
if errorlevel 2 (
    echo Для следующего запуска используйте start_windows.bat.
    pause
    exit /b 0
)

".venv\Scripts\python.exe" -m userbot.userbot
set "FAUST_EXIT=%ERRORLEVEL%"
echo.
echo Faust Tool завершил работу с кодом %FAUST_EXIT%.
pause
exit /b %FAUST_EXIT%

:install_failed
echo.
echo Установка остановлена из-за ошибки выше.
pause
exit /b 1

:find_python
set "PYTHON_BIN="
set "PYTHON_ARGS="
where py >nul 2>&1
if not errorlevel 1 (
    py -3 -c "import sys; raise SystemExit(0 if sys.version_info ^>= (3, 10) else 1)" >nul 2>&1
    if not errorlevel 1 (
        set "PYTHON_BIN=py"
        set "PYTHON_ARGS=-3"
        exit /b 0
    )
)
where python >nul 2>&1
if not errorlevel 1 (
    python -c "import sys; raise SystemExit(0 if sys.version_info ^>= (3, 10) else 1)" >nul 2>&1
    if not errorlevel 1 (
        set "PYTHON_BIN=python"
        set "PYTHON_ARGS="
    )
)
exit /b 0
