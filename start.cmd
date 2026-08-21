@echo off
setlocal
cd /d "%~dp0"
set "PHOTO_WEB_RESTART_MODE=local"
set "PHOTO_WEB_SUPERVISED=1"

echo Short drama ad image library - local server
echo URL: http://127.0.0.1:8000
echo Supervisor: enabled, Uvicorn workers: 1
echo.

powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\run-photo-web-supervisor.ps1"
set "PHOTO_WEB_EXIT_CODE=%ERRORLEVEL%"

echo.
if not "%PHOTO_WEB_EXIT_CODE%"=="0" echo Supervisor stopped with exit code %PHOTO_WEB_EXIT_CODE%.
pause
endlocal & exit /b %PHOTO_WEB_EXIT_CODE%
