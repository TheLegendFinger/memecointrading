# Start trading. REAL money - read LIVE_TRADING.md before using this.
#
#   powershell -ExecutionPolicy Bypass -File scripts\run.ps1

. (Join-Path $PSScriptRoot '_common.ps1')

Test-Venv

$configArgs = @()
if (Test-Path -LiteralPath 'config.yaml') { $configArgs = @('--config', 'config.yaml') }

if (-not (Install-LiveExtras)) {
    Write-Fail "Could not install the Solana packages. Try running this by hand to see why:`n    $script:Py -m pip install -r requirements-live.txt"
}

Invoke-Memebot @configArgs wallet
if ($LASTEXITCODE -ne 0) { Write-Fail 'The wallet is not ready to trade - see above.' }

Write-Host ''
Write-Host '  You are about to trade REAL money.' -ForegroundColor Yellow
Write-Host '  This bot buys brand-new memecoins. Many go to zero. It can lose'
Write-Host '  everything in the wallet, and it keeps trading until you stop it.'
Write-Host ''
$answer = Read-Host '  Type LIVE to start trading for real'
if ($answer -cne 'LIVE') { Write-Fail 'Not started.' }

# This process only: it disappears when the window closes, so nothing else can
# trade for real without going through here.
$env:LIVE_TRADING_CONFIRM = 'I_UNDERSTAND_THE_RISK'
$args += '-y'

Write-Host ''
Invoke-Memebot @configArgs run @args
exit $LASTEXITCODE
