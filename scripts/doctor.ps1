# Check that the market data feeds are reachable from this machine.
#
#   powershell -ExecutionPolicy Bypass -File scripts\doctor.ps1

. (Join-Path $PSScriptRoot '_common.ps1')

Test-Venv

$configArgs = @()
if (Test-Path -LiteralPath 'config.yaml') { $configArgs = @('--config', 'config.yaml') }

Invoke-Memebot @configArgs doctor @args
exit $LASTEXITCODE
