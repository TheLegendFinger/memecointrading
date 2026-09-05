# One-time setup for Windows (PowerShell).
#
#   powershell -ExecutionPolicy Bypass -File scripts\setup.ps1
#
# Creates a virtual environment, installs dependencies, and copies the example
# config. Safe to re-run.

# Note: native commands write to stderr routinely (pip progress, python
# tracebacks). Windows PowerShell treats that as fatal under 'Stop', so this
# script checks exit codes instead.
$ErrorActionPreference = 'Continue'
$ProgressPreference = 'SilentlyContinue'

$RepoRoot = Split-Path $PSScriptRoot -Parent
Set-Location -LiteralPath $RepoRoot

# Shared helpers (Resolve-VenvPython, Write-Fail). Safe to load before the
# virtual environment exists - it simply resolves to nothing.
. (Join-Path $PSScriptRoot '_common.ps1')

# Find a usable Python. `py -3` first (the Windows launcher), then the plain
# names. The version check writes nothing to stderr on success or failure.
function Find-Python {
    $candidates = @(
        @{ Exe = 'py';      Args = @('-3') },
        @{ Exe = 'python';  Args = @() },
        @{ Exe = 'python3'; Args = @() }
    )
    foreach ($candidate in $candidates) {
        if (-not (Get-Command $candidate.Exe -ErrorAction SilentlyContinue)) { continue }
        $probe = 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)'
        $callArgs = @($candidate.Args) + @('-c', $probe)
        & $candidate.Exe @callArgs 2>&1 | Out-Null
        if ($LASTEXITCODE -eq 0) { return $candidate }
    }
    return $null
}

$python = Find-Python
if (-not $python) {
    Write-Host ''
    Write-Host '  Python 3.9 or newer was not found.' -ForegroundColor Red
    Write-Host "  Install it from https://www.python.org/downloads/ (tick 'Add python.exe to PATH'),"
    Write-Host '  or run:  winget install Python.Python.3.12'
    Write-Host '  Then close this window, open a new one, and run setup again.'
    Write-Host ''
    exit 1
}
Write-Host "  Using $($python.Exe) $($python.Args -join ' ')" -ForegroundColor Cyan

$venvPy = Resolve-VenvPython -Root $RepoRoot

if (-not $venvPy) {
    Write-Host '  Creating virtual environment (.venv)...' -ForegroundColor Cyan
    $venvArgs = @($python.Args) + @('-m', 'venv', '.venv')
    & $python.Exe @venvArgs
    $venvPy = Resolve-VenvPython -Root $RepoRoot
    if ($LASTEXITCODE -ne 0 -or -not $venvPy) {
        Write-Fail "Could not create the virtual environment. Try by hand:`n    $($python.Exe) -m venv .venv"
    }
}

Write-Host '  Installing dependencies (this takes a minute)...' -ForegroundColor Cyan
& $venvPy -m pip install --upgrade pip --quiet --disable-pip-version-check
& $venvPy -m pip install -r requirements.txt --quiet --disable-pip-version-check
if ($LASTEXITCODE -ne 0) {
    Write-Fail "Dependency installation failed. Run this to see the full error:`n    $venvPy -m pip install -r requirements.txt"
}

# Confirm the package actually imports - a silent half-install is worse than a
# loud failure.
& $venvPy -c 'import memebot' 2>&1 | Out-Null
if ($LASTEXITCODE -ne 0) {
    Write-Fail "Installed, but 'import memebot' failed. Are you running this from the repository folder?"
}

if (-not (Test-Path -LiteralPath 'config.yaml')) {
    Copy-Item 'config.example.yaml' 'config.yaml'
    Write-Host '  Created config.yaml from the example.' -ForegroundColor Green
}
if (-not (Test-Path -LiteralPath '.env')) {
    Copy-Item '.env.example' '.env'
    Write-Host '  Created .env from the example.' -ForegroundColor Green
}

Write-Host ''
Write-Host '  Setup complete.' -ForegroundColor Green
Write-Host ''
Write-Host '  Check the market feeds :  powershell -ExecutionPolicy Bypass -File scripts\doctor.ps1'
Write-Host '  Paper trade            :  powershell -ExecutionPolicy Bypass -File scripts\run.ps1'
Write-Host '  Dashboard              :  powershell -ExecutionPolicy Bypass -File scripts\dashboard.ps1'
Write-Host '  Trade for real         :  powershell -ExecutionPolicy Bypass -File scripts\live.ps1'
Write-Host ''
