@echo off
setlocal
chcp 65001 >nul

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\uninstall_native_host.ps1"
if errorlevel 1 (
  echo.
  echo Не удалось удалить локальный помощник StreamDock.
  pause
  exit /b 1
)
echo.
pause
