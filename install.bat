@echo off
REM ==========================================================================
REM  install.bat - one-time setup for Enkelvoud PC
REM
REM  Put a private Python in runtime\python and install requirements.txt into
REM  it, so run.bat works on a PC that has never had Python installed. It does
REM  not touch the system Python, PATH or the registry; delete runtime\ to undo.
REM
REM  Usage:
REM    install.bat                  normal install (internet: ~60 MB down,
REM                                 ~250 MB on disk)
REM    install.bat -Force           throw runtime\python away and download again
REM    install.bat -PythonVersion 3.12.16
REM
REM  Offline: download python-3.13.15-embed-amd64.zip on another PC, put it in
REM  vendor\ next to this file, then run install.bat.
REM ==========================================================================

setlocal
REM pushd, not cd /d: on a UNC path (\\server\share) cmd cannot cd and would
REM stay in C:\Windows, so tools\setup_runtime.ps1 would not be found.
pushd "%~dp0"

echo ================================================================
echo  Enkelvoud PC server - install
echo ================================================================
echo.

powershell -NoProfile -ExecutionPolicy Bypass -File "tools\setup_runtime.ps1" %*
set "RC=%errorlevel%"

echo.
if not "%RC%"=="0" (
    echo ================================================================
    echo  Install FAILED ^(code %RC%^). The messages above say why.
    echo  Fix the cause and run install.bat again.
    echo ================================================================
) else (
    echo ================================================================
    echo  Done. Start the server with run.bat ^(double-click^).
    echo ================================================================
)

echo.
echo Press any key to close this window...
pause >nul
popd
exit /b %RC%
