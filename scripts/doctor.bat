@echo off
REM Check that the market data feeds are reachable from this machine.
setlocal
cd /d "%~dp0.."
if not exist ".venv\Scripts\python.exe" (
  echo   Run scripts\setup.ps1 first.
  exit /b 1
)
set CONFIG=
if exist "config.yaml" set CONFIG=--config config.yaml
".venv\Scripts\python.exe" -m memebot doctor %CONFIG% %*
echo.
pause
endlocal
