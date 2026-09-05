@echo off
REM Serve the dashboard at http://localhost:8000 against the local state file.
setlocal
cd /d "%~dp0.."

if not exist ".venv\Scripts\python.exe" (
  echo.
  echo   No virtual environment found. Run this first:
  echo     powershell -ExecutionPolicy Bypass -File scripts\setup.ps1
  echo.
  exit /b 1
)

start "" http://localhost:8000
".venv\Scripts\python.exe" scripts\dev_server.py --port 8000 %*
endlocal
