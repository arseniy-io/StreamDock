@echo off
setlocal
chcp 65001 >nul

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\update.ps1" %*
set "STREAMDOCK_EXIT=%ERRORLEVEL%"

if not "%STREAMDOCK_EXIT%"=="0" (
  echo.
  echo Обновление StreamDock не завершено. Подробности указаны выше.
) else (
  echo.
  echo Зависимости и локальный помощник StreamDock обновлены.
)

if not defined STREAMDOCK_NO_PAUSE pause
exit /b %STREAMDOCK_EXIT%
