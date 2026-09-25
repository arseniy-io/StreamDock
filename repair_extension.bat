@echo off
setlocal
chcp 65001 >nul

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\install_native_host.ps1"
set "STREAMDOCK_EXIT=%ERRORLEVEL%"

if not "%STREAMDOCK_EXIT%"=="0" (
  echo.
  echo Не удалось восстановить помощник Chrome. Исправьте ошибку выше и повторите попытку.
) else (
  echo.
  echo Помощник Chrome восстановлен. Перезагрузите StreamDock на странице chrome://extensions.
)

if not defined STREAMDOCK_NO_PAUSE pause
exit /b %STREAMDOCK_EXIT%
