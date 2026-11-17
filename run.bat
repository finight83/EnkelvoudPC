@echo off
REM run.bat - Enkelvoud PC server launcher
REM Port 80 usually needs Administrator. Run this file from an elevated
REM cmd prompt if you get "Failed to bind port 80".

setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo [run.bat] creating venv...
    py -3 -m venv .venv
    .venv\Scripts\python.exe -m pip install --upgrade pip
    .venv\Scripts\python.exe -m pip install -r requirements.txt
)

.venv\Scripts\python.exe -m enkelvoud %*

endlocal