$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot

$python = Get-Command python -ErrorAction SilentlyContinue
if (-not $python) {
    Write-Host ""
    Write-Host "  Python was not found on PATH." -ForegroundColor Red
    Write-Host "  Install Python 3.11+ from https://www.python.org/downloads/"
    Write-Host "  and tick 'Add python.exe to PATH' during setup."
    Write-Host ""
    Read-Host "Press Enter to close"
    exit 1
}

& $python.Source app\main.py @args
$code = $LASTEXITCODE
if ($code -ne 0) {
    Write-Host ""
    Write-Host "  wow-vendor-flip exited with code $code" -ForegroundColor Red
    Read-Host "Press Enter to close"
    exit $code
}
