@echo off
setlocal
chcp 65001 >nul

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\start.ps1" %*
set "STREAMDOCK_EXIT=%ERRORLEVEL%"

if not "%STREAMDOCK_EXIT%"=="0" (
  echo.
  echo StreamDock не запущен. Подробности указаны выше.
  if not defined STREAMDOCK_NO_PAUSE pause
)
exit /b %STREAMDOCK_EXIT%
