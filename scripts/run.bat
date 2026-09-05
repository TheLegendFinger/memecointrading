@echo off
REM Start the bot in paper mode. Ctrl+C stops it.
REM
REM   scripts\run.bat                 paper trading, config.yaml
REM   scripts\run.bat --dry-run       evaluate only, place no orders
REM
setlocal
cd /d "%~dp0.."

if not exist ".venv\Scripts\python.exe" (
  echo.
  echo   No virtual environment found. Run this first:
  echo     powershell -ExecutionPolicy Bypass -File scripts\setup.ps1
  echo.
  exit /b 1
)

set CONFIG=
if exist "config.yaml" set CONFIG=--config config.yaml

echo.
echo   Starting memebot (paper mode). Press Ctrl+C to stop.
echo.
".venv\Scripts\python.exe" -m memebot run %CONFIG% %*
endlocal
