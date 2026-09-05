# Guided launcher for LIVE trading with real funds.
#
#   powershell -ExecutionPolicy Bypass -File scripts\live.ps1
#
# It checks the wallet, the balance and the market feeds, makes you confirm,
# and only then arms live trading - for this one process. The interlock is
# never written to disk, so nothing else you run can trade for real by
# accident.

$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)

$py = ".\.venv\Scripts\python.exe"

function Fail($message) {
    Write-Host ""
    Write-Host "  $message" -ForegroundColor Red
    Write-Host ""
    exit 1
}

function Rule { Write-Host ("  " + ("-" * 66)) -ForegroundColor DarkGray }

if (-not (Test-Path $py)) {
    Fail "No virtual environment. Run this first:`n    powershell -ExecutionPolicy Bypass -File scripts\setup.ps1"
}

Write-Host ""
Write-Host "  LIVE TRADING SETUP" -ForegroundColor Yellow
Rule

# ----------------------------------------------------------------- 0. config
# Live settings live in their own file so paper settings stay untouched.
if (-not (Test-Path "config.live.yaml")) {
    Copy-Item "config.live.example.yaml" "config.live.yaml"
    Write-Host "  Created config.live.yaml (small-wallet settings)." -ForegroundColor Green
}
Write-Host "  [ok] Using config.live.yaml" -ForegroundColor Green

# ---------------------------------------------------------------- 1. packages
& $py -c "import solders" 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host "  Installing the Solana packages (solders, base58)..." -ForegroundColor Cyan
    & $py -m pip install -r requirements-live.txt --quiet
    if ($LASTEXITCODE -ne 0) { Fail "Could not install the live trading packages." }
}
Write-Host "  [ok] Solana packages installed" -ForegroundColor Green

# ------------------------------------------------------------------ 2. wallet
$walletOut = & $py -m memebot wallet 2>&1
if ($LASTEXITCODE -ne 0 -and ($walletOut -join "`n") -match "No wallet configured") {
    Write-Host ""
    Write-Host "  No wallet is configured yet." -ForegroundColor Yellow
    Write-Host "  A new one can be created and saved to .env (which is gitignored)."
    $answer = Read-Host "  Create a new burner wallet now? [y/N]"
    if ($answer -notmatch '^(y|yes)$') {
        Fail "Nothing to trade with. Add SOLANA_PRIVATE_KEY to .env, or re-run and say yes."
    }
    & $py -m memebot wallet --new --save
    if ($LASTEXITCODE -ne 0) { Fail "Wallet creation failed." }
    Write-Host ""
    Write-Host "  Send some SOL to the address above, then run this script again." -ForegroundColor Yellow
    Write-Host "  Start small - whatever you would not mind losing entirely."
    Write-Host ""
    exit 0
}

Write-Host ""
& $py -m memebot wallet
if ($LASTEXITCODE -ne 0) {
    Fail "The wallet is not ready to trade. Fix what it reported above, then re-run."
}

# ----------------------------------------------------------------- 3. feeds
Rule
Write-Host "  Checking the market feeds..." -ForegroundColor Cyan
& $py -m memebot doctor --quick --config config.live.yaml
if ($LASTEXITCODE -ne 0) {
    Write-Host ""
    $answer = Read-Host "  Some checks failed. Start anyway? [y/N]"
    if ($answer -notmatch '^(y|yes)$') { Fail "Stopped." }
}

# --------------------------------------------------------------- 4. confirm
Rule
Write-Host ""
Write-Host "  You are about to trade REAL money." -ForegroundColor Yellow
Write-Host "  This bot buys brand-new memecoins. Many go to zero. It can lose"
Write-Host "  everything in the wallet, and it will keep trading until you stop it."
Write-Host ""
Write-Host "  Stop it any time with Ctrl+C. Positions stay open - close them with:"
Write-Host "    $py -m memebot liquidate --config config.live.yaml"
Write-Host ""
$answer = Read-Host "  Type LIVE to start trading for real"
if ($answer -cne "LIVE") { Fail "Not started." }

# ------------------------------------------------------------------ 5. run
# Process-scoped only: it disappears when this window closes, so no other
# command can trade for real without going through this script.
$env:LIVE_TRADING_CONFIRM = "I_UNDERSTAND_THE_RISK"
$env:MEMEBOT_MODE = "live"

# Build the argument list as an array - an empty string would otherwise be
# passed through as a stray empty argument.
$runArgs = @("-m", "memebot", "run", "--mode", "live", "-y", "--config", "config.live.yaml")
if ($args.Count -gt 0) { $runArgs += $args }

Write-Host ""
Write-Host "  Starting. Ctrl+C to stop." -ForegroundColor Green
Write-Host ""
& $py @runArgs
