# run.ps1 - Enkelvoud PC server launcher
#
# Same precedence as run.bat: runtime\python > .venv > a Python 3.10+ found as
# `py -3` / `python` on PATH. The private runtime is built on first use by
# tools\setup_runtime.ps1 -- no system Python install needed.
#
#   powershell -ExecutionPolicy Bypass -File .\run.ps1 --port 8080

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$runtimePy    = Join-Path $PSScriptRoot "runtime\python\python.exe"
$runtimeStamp = Join-Path $PSScriptRoot "runtime\runtime.json"
$setupScript  = Join-Path $PSScriptRoot "tools\setup_runtime.ps1"

if (-not (Test-Path $runtimePy) -or -not (Test-Path $runtimeStamp)) {
    Write-Host "[run.ps1] no bundled Python yet - setting up runtime\python (one time)..."
    # A child process, so the setup script's exit code cannot end this script
    # before the fallbacks below get a chance.
    & powershell -NoProfile -ExecutionPolicy Bypass -File $setupScript
}

if (Test-Path $runtimePy) {
    & $runtimePy -m enkelvoud @args
} elseif (Test-Path ".\.venv\Scripts\python.exe") {
    & ".\.venv\Scripts\python.exe" -m enkelvoud @args
} else {
    Write-Host "[run.ps1] no bundled Python and no .venv; trying 'py -3'"
    & py -3 -m enkelvoud @args
}
exit $LASTEXITCODE
