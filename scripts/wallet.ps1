# Show the live trading wallet: address, balance, and what it can trade with.
#
#   powershell -ExecutionPolicy Bypass -File scripts\wallet.ps1
#   powershell -ExecutionPolicy Bypass -File scripts\wallet.ps1 -New

param([switch]$New, [switch]$Save)

$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)
$py = ".\.venv\Scripts\python.exe"

if (-not (Test-Path $py)) {
    Write-Host "  Run scripts\setup.ps1 first." -ForegroundColor Red
    exit 1
}

& $py -c "import solders" 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host "  Installing the Solana packages..." -ForegroundColor Cyan
    & $py -m pip install -r requirements-live.txt --quiet
}

$config = @()
if (Test-Path "config.live.yaml") { $config = @("--config", "config.live.yaml") }

if ($New) {
    if ($Save) { & $py -m memebot wallet --new --save } else { & $py -m memebot wallet --new }
} else {
    & $py -m memebot @config wallet
}
