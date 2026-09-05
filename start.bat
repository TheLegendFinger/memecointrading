@echo off
REM Double-click this to open the memebot menu.
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "scripts\start.ps1"
if errorlevel 1 pause
