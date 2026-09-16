@echo off
setlocal EnableExtensions
chcp 65001 >nul
cd /d "%~dp0"
title Faust Tool v3

if not exist ".venv\Scripts\python.exe" (
    echo Faust Tool ещё не установлен. Запускаю установщик...
    call "%~dp0install_windows.bat"
    if errorlevel 1 exit /b 1
    exit /b 0
)

".venv\Scripts\python.exe" -m userbot.userbot
set "FAUST_EXIT=%ERRORLEVEL%"
echo.
echo Faust Tool завершил работу с кодом %FAUST_EXIT%.
pause
exit /b %FAUST_EXIT%
