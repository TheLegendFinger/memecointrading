# Paper trading against the live market. Ctrl+C stops it.
#
#   powershell -ExecutionPolicy Bypass -File scripts\run.ps1
#   powershell -ExecutionPolicy Bypass -File scripts\run.ps1 --dry-run

. (Join-Path $PSScriptRoot '_common.ps1')

Test-Venv

$configArgs = @()
if (Test-Path -LiteralPath 'config.yaml') { $configArgs = @('--config', 'config.yaml') }

Write-Host ''
Write-Host '  Paper trading the live market. Ctrl+C to stop.' -ForegroundColor Green
Write-Host ''
Invoke-Memebot @configArgs run @args
exit $LASTEXITCODE
