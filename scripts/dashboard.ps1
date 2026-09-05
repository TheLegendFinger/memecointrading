# Serve the dashboard at http://localhost:8000 against the local state file.
#
#   powershell -ExecutionPolicy Bypass -File scripts\dashboard.ps1
#   powershell -ExecutionPolicy Bypass -File scripts\dashboard.ps1 --db data\memebot-live.sqlite3

. (Join-Path $PSScriptRoot '_common.ps1')

Test-Venv

Start-Process 'http://localhost:8000'
& $script:Py (Join-Path 'scripts' 'dev_server.py') --port 8000 @args
exit $LASTEXITCODE
