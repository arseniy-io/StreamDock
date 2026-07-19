@echo off
setlocal
chcp 65001 >nul

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\install.ps1" %*
set "STREAMDOCK_EXIT=%ERRORLEVEL%"

if not "%STREAMDOCK_EXIT%"=="0" (
  echo.
  echo Установка StreamDock не завершена. Исправьте указанную выше проблему и запустите install.bat ещё раз.
) else (
  echo.
  echo Установка StreamDock завершена.
)

if not defined STREAMDOCK_NO_PAUSE pause
exit /b %STREAMDOCK_EXIT%
