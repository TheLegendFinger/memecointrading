# Shared helpers for the memebot PowerShell scripts.
#
# The important thing this file exists for: Windows PowerShell turns ANY output
# a native command writes to stderr into a terminating error when
# $ErrorActionPreference is 'Stop'. python, pip and the bot itself all write
# perfectly normal progress and log lines to stderr, so a script that used
# 'Stop' would die on the bot's first log line. Failures are therefore detected
# from exit codes, explicitly, everywhere.

$ErrorActionPreference = 'Continue'
$ProgressPreference = 'SilentlyContinue'   # no progress bars over pip output

# Repo root is the parent of the scripts folder this file lives in.
$script:RepoRoot = Split-Path $PSScriptRoot -Parent
Set-Location -LiteralPath $script:RepoRoot

# Where the virtual environment's interpreter lives. Join-Path (rather than a
# literal ".\.venv\Scripts\python.exe") keeps these scripts testable off
# Windows, where venv writes bin/python instead of Scripts\python.exe.
function Resolve-VenvPython {
    param([string]$Root = $script:RepoRoot)
    $venv = Join-Path $Root '.venv'
    $windows = Join-Path (Join-Path $venv 'Scripts') 'python.exe'
    if (Test-Path -LiteralPath $windows) { return $windows }
    $unix = Join-Path (Join-Path $venv 'bin') 'python'
    if (Test-Path -LiteralPath $unix) { return $unix }
    return $null
}

$script:Py = Resolve-VenvPython

function Write-Rule { Write-Host ('  ' + ('-' * 66)) -ForegroundColor DarkGray }

function Write-Fail {
    param([string]$Message)
    Write-Host ''
    Write-Host "  $Message" -ForegroundColor Red
    Write-Host ''
    exit 1
}

function Test-Venv {
    if (-not $script:Py) {
        Write-Fail "No virtual environment found. Run this first:`n    powershell -ExecutionPolicy Bypass -File scripts\setup.ps1"
    }
}

# Is a Python package importable? Prints nothing either way - a bare
# `python -c "import x"` would dump a traceback to stderr, which is exactly
# what breaks Windows PowerShell.
function Test-PyModule {
    param([Parameter(Mandatory = $true)][string]$Name)
    $probe = 'import importlib.util as u, sys; sys.exit(0 if u.find_spec(sys.argv[1]) else 1)'
    & $script:Py -c $probe $Name 2>&1 | Out-Null
    return ($LASTEXITCODE -eq 0)
}

# Run the bot, letting its output through to the console. Deliberately returns
# nothing - check $LASTEXITCODE after calling it. (Returning the exit code would
# put it on the pipeline, and suppressing that with Out-Null would swallow the
# bot's own output, which is the whole point of running it.)
function Invoke-Memebot {
    param([Parameter(ValueFromRemainingArguments = $true)][string[]]$Arguments)
    & $script:Py -m memebot @Arguments
}

function Install-LiveExtras {
    if (Test-PyModule -Name 'solders') { return $true }
    Write-Host '  Installing the Solana packages (solders, base58)...' -ForegroundColor Cyan
    & $script:Py -m pip install -r requirements-live.txt --quiet --disable-pip-version-check
    if ($LASTEXITCODE -ne 0) { return $false }
    return (Test-PyModule -Name 'solders')
}

function Read-YesNo {
    param([string]$Prompt)
    $answer = Read-Host "  $Prompt [y/N]"
    return ($answer -match '^(y|yes)$')
}
