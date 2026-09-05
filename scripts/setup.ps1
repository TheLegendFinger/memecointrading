# One-time setup for Windows (PowerShell).
#
#   powershell -ExecutionPolicy Bypass -File scripts\setup.ps1
#
# Creates a virtual environment, installs dependencies, and copies the example
# config. Safe to re-run.

$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)

function Find-Python {
    foreach ($candidate in @("py -3", "python", "python3")) {
        $parts = $candidate.Split(" ")
        $exe = $parts[0]
        if (Get-Command $exe -ErrorAction SilentlyContinue) {
            try {
                $version = & $exe $parts[1..($parts.Length - 1)] --version 2>&1
                if ($version -match "Python 3\.(9|1[0-9])") { return $candidate }
            } catch { }
        }
    }
    return $null
}

$python = Find-Python
if (-not $python) {
    Write-Host ""
    Write-Host "Python 3.9+ was not found." -ForegroundColor Red
    Write-Host "Install it from https://www.python.org/downloads/ (tick 'Add python.exe to PATH')"
    Write-Host "or run:  winget install Python.Python.3.12"
    exit 1
}
Write-Host "Using $python" -ForegroundColor Cyan

if (-not (Test-Path ".venv")) {
    Write-Host "Creating virtual environment (.venv)..." -ForegroundColor Cyan
    $parts = $python.Split(" ")
    & $parts[0] $parts[1..($parts.Length - 1)] -m venv .venv
}

Write-Host "Installing dependencies..." -ForegroundColor Cyan
& ".\.venv\Scripts\python.exe" -m pip install --upgrade pip --quiet
& ".\.venv\Scripts\python.exe" -m pip install -r requirements.txt --quiet

if (-not (Test-Path "config.yaml")) {
    Copy-Item "config.example.yaml" "config.yaml"
    Write-Host "Created config.yaml from the example." -ForegroundColor Green
}
if (-not (Test-Path ".env")) {
    Copy-Item ".env.example" ".env"
    Write-Host "Created .env from the example." -ForegroundColor Green
}

Write-Host ""
Write-Host "Setup complete." -ForegroundColor Green
Write-Host ""
Write-Host "  Check the market feeds :  .\.venv\Scripts\python.exe -m memebot doctor"
Write-Host "  See what it would buy  :  .\.venv\Scripts\python.exe -m memebot scan"
Write-Host "  Start paper trading    :  scripts\run.bat"
Write-Host "  Open the dashboard     :  scripts\dashboard.bat"
Write-Host ""
