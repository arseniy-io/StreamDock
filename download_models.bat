@echo off
setlocal
chcp 65001 >nul

if not exist "%~dp0.venv\Scripts\python.exe" (
  echo Сначала запустите install.bat.
  if not defined STREAMDOCK_NO_PAUSE pause
  exit /b 1
)

"%~dp0.venv\Scripts\python.exe" "%~dp0scripts\download_models.py" %*
set "STREAMDOCK_EXIT=%ERRORLEVEL%"

if not "%STREAMDOCK_EXIT%"=="0" (
  echo.
  echo Не удалось загрузить все выбранные модели. Уже загруженные файлы сохранятся.
)

if not defined STREAMDOCK_NO_PAUSE pause
exit /b %STREAMDOCK_EXIT%
