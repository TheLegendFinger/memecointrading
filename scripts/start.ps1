# The front door. Opens the menu - everything is a number from there.
#
#   powershell -ExecutionPolicy Bypass -File scripts\start.ps1
#
# If the project has not been set up yet, this offers to do it first.

. (Join-Path $PSScriptRoot '_common.ps1')

if (-not $script:Py) {
    Write-Host ''
    Write-Host '  memebot is not set up on this machine yet.' -ForegroundColor Yellow
    Write-Host '  Setup creates a private Python environment and installs what it needs.'
    Write-Host ''
    if (-not (Read-YesNo 'Run setup now?')) {
        Write-Fail "Nothing to run yet. When you are ready:`n    powershell -ExecutionPolicy Bypass -File scripts\setup.ps1"
    }
    & (Join-Path $PSScriptRoot 'setup.ps1')
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    $script:Py = Resolve-VenvPython
    if (-not $script:Py) { Write-Fail 'Setup did not finish. See the messages above.' }
}

Invoke-Memebot menu
exit $LASTEXITCODE
