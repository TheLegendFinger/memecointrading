# Show the live trading wallet: address, balance, and what it can trade with.
#
#   powershell -ExecutionPolicy Bypass -File scripts\wallet.ps1
#   powershell -ExecutionPolicy Bypass -File scripts\wallet.ps1 -New -Save

param([switch]$New, [switch]$Save)

. (Join-Path $PSScriptRoot '_common.ps1')

Test-Venv

if (-not (Install-LiveExtras)) {
    Write-Fail "Could not install the Solana packages. Try running this by hand to see why:`n    $script:Py -m pip install -r requirements-live.txt"
}

$configArgs = @()
if (Test-Path -LiteralPath 'config.live.yaml') { $configArgs = @('--config', 'config.live.yaml') }

if ($New) {
    if ($Save) {
        Invoke-Memebot @configArgs wallet --new --save
    } else {
        Invoke-Memebot @configArgs wallet --new
    }
    exit $LASTEXITCODE
}

Invoke-Memebot @configArgs wallet
exit $LASTEXITCODE
