# Guided launcher for LIVE trading with real funds.
#
#   powershell -ExecutionPolicy Bypass -File scripts\live.ps1
#
# It checks the wallet, the balance and the market feeds, makes you confirm,
# and only then arms live trading - for this one process. The interlock is
# never written to disk, so nothing else you run can trade for real by
# accident.

. (Join-Path $PSScriptRoot '_common.ps1')

Test-Venv

Write-Host ''
Write-Host '  LIVE TRADING SETUP' -ForegroundColor Yellow
Write-Rule

# ----------------------------------------------------------------- 1. config
# Live settings live in their own file so paper settings stay untouched.
if (-not (Test-Path -LiteralPath 'config.live.yaml')) {
    Copy-Item 'config.live.example.yaml' 'config.live.yaml'
    if (-not (Test-Path -LiteralPath 'config.live.yaml')) {
        Write-Fail 'Could not create config.live.yaml from the example.'
    }
    Write-Host '  Created config.live.yaml (small-wallet settings).' -ForegroundColor Green
}
Write-Host '  [ok] Using config.live.yaml' -ForegroundColor Green

# --------------------------------------------------------------- 2. packages
if (-not (Install-LiveExtras)) {
    Write-Fail "Could not install the Solana packages. Try running this by hand to see why:`n    $script:Py -m pip install -r requirements-live.txt"
}
Write-Host '  [ok] Solana packages installed' -ForegroundColor Green

# ------------------------------------------------------------------- 3. arm
# Set the interlock now, for THIS PROCESS ONLY, so the checks below actually
# validate the live path (key, balance, RPC) instead of reporting "not armed".
# Arming is not trading: nothing is ordered until you type LIVE at step 6, and
# this variable disappears when the window closes.
$env:LIVE_TRADING_CONFIRM = 'I_UNDERSTAND_THE_RISK'

# ----------------------------------------------------------------- 4. wallet
Write-Host ''
Invoke-Memebot --config config.live.yaml wallet
$walletCode = $LASTEXITCODE

# 2 = nothing configured, so offering to create one is right.
# 3 = a wallet exists but cannot trade yet (unfunded, or the RPC is down).
#     Never offer to create another - the existing one may hold funds.
if ($walletCode -eq 2) {
    Write-Host ''
    if (Read-YesNo 'No wallet yet. Create a new burner wallet now?') {
        Invoke-Memebot wallet --new --save
        if ($LASTEXITCODE -ne 0) { Write-Fail 'Wallet creation failed.' }
        Write-Host ''
        Write-Host '  Send some SOL to the address above, then run this script again.' -ForegroundColor Yellow
        Write-Host '  Start small - whatever you would not mind losing entirely.'
        Write-Host ''
        exit 0
    }
    Write-Fail 'Nothing to trade with. Add SOLANA_PRIVATE_KEY to .env, or re-run and say yes.'
}
elseif ($walletCode -ne 0) {
    Write-Fail 'The wallet cannot trade yet - see above. Fix that, then run this again.'
}

# ------------------------------------------------------------------ 5. feeds
Write-Rule
Write-Host '  Checking the market feeds...' -ForegroundColor Cyan
Invoke-Memebot --config config.live.yaml doctor --quick
if ($LASTEXITCODE -ne 0) {
    Write-Host ''
    Write-Host '  Some checks failed. The bot will run but may see an empty market.' -ForegroundColor Yellow
    if (-not (Read-YesNo 'Continue anyway?')) { Write-Fail 'Stopped.' }
}

# ---------------------------------------------------------------- 6. confirm
Write-Rule
Write-Host ''
Write-Host '  You are about to trade REAL money.' -ForegroundColor Yellow
Write-Host '  This bot buys brand-new memecoins. Many go to zero. It can lose'
Write-Host '  everything in the wallet, and it will keep trading until you stop it.'
Write-Host ''
Write-Host '  Stop it any time with Ctrl+C. Positions stay open - close them with:'
Write-Host "    $script:Py -m memebot liquidate --config config.live.yaml"
Write-Host ''
$answer = Read-Host '  Type LIVE to start trading for real'
if ($answer -cne 'LIVE') { Write-Fail 'Not started.' }

# -------------------------------------------------------------------- 7. run
Write-Host ''
Write-Host '  Starting. Ctrl+C to stop.' -ForegroundColor Green
Write-Host ''
Invoke-Memebot --config config.live.yaml --mode live run -y @args
exit $LASTEXITCODE
