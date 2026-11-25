@echo off
REM ==========================================================================
REM  run.bat - Enkelvoud PC server launcher
REM
REM  Double-click it. It does NOT need Python to be installed first: on the
REM  first run it builds a private Python in runtime\python from the official
REM  Windows "embeddable" package and installs requirements.txt into it. That
REM  is one download of about 60 MB, about 250 MB on disk; every later run is
REM  offline and instant.
REM  See tools\setup_runtime.ps1 for what that does, and README.md ("No Python
REM  to install") for the offline/vendor\ route.
REM
REM  Precedence: runtime\python  >  .venv  >  a Python 3.10+ on this PC.
REM  Set ENKELVOUD_NO_BOOTSTRAP=1 to skip the bundled-Python install entirely.
REM
REM  Why this file is written defensively: double-clicking it opens a console
REM  that closes the moment the script ends, so every way it can fail used to
REM  look identical -- a window that blinks and disappears. It now explains
REM  itself and waits for a key press before closing, whatever happens.
REM ==========================================================================

setlocal
cd /d "%~dp0"

echo ================================================================
echo  Enkelvoud PC server
echo ================================================================
echo.

set "RUNTIME_PY=runtime\python\python.exe"
set "RUNTIME_STAMP=runtime\runtime.json"
set "PY="

REM ---- 1. A private Python from install.bat or an earlier run? -----------
if not exist "%RUNTIME_PY%" goto :no_runtime
if not exist "%RUNTIME_STAMP%" goto :no_runtime
set "PY=%RUNTIME_PY%"
echo [run.bat] Python: bundled runtime\python
goto :have_python

:no_runtime
REM ---- 2. Build it now, one time. ---------------------------------------
if /i "%ENKELVOUD_NO_BOOTSTRAP%"=="1" (
    echo [run.bat] ENKELVOUD_NO_BOOTSTRAP=1: not installing the bundled Python.
    goto :system_python
)

echo [run.bat] No bundled Python yet: setting up runtime\python.
echo [run.bat] One time only: ~60 MB download, ~250 MB on disk.
echo.
powershell -NoProfile -ExecutionPolicy Bypass -File "tools\setup_runtime.ps1"
if errorlevel 1 (
    echo.
    echo [run.bat] Setting up the bundled Python failed -- the messages above say why.
    echo [run.bat] Falling back to a Python already installed on this PC.
    echo.
    goto :system_python
)
if not exist "%RUNTIME_PY%" goto :system_python
if not exist "%RUNTIME_STAMP%" goto :system_python
set "PY=%RUNTIME_PY%"
echo.
echo [run.bat] Bundled Python ready.
goto :have_python

:system_python
REM ---- 3. Any Python 3.10 or newer already on this PC. ------------------
REM `py -3` only works when a Python is registered with the Windows launcher;
REM on some installs (one-user installs, Store installs) it says "No installed
REM Python found!" and the venv step fails. Try the launcher, then PATH, then
REM the usual install folders. Each probe prints sys.executable, so what we
REM keep is a real path -- `"py -3" -m enkelvoud` would not run.
call :find_system_python
if not defined PY (
    echo.
    echo [run.bat] No bundled Python and no Python 3.10 or newer on this PC.
    echo.
    echo   The bundled Python could not be installed; without internet on the
    echo   first run that is expected. Two ways to fix it:
    echo.
    echo   1. Get this PC online and run install.bat again - it retries the download.
    echo   2. On a PC with internet, download
    echo         https://www.python.org/ftp/python/3.13.15/python-3.13.15-embed-amd64.zip
    echo      and put that zip in a folder named "vendor" next to this file.
    echo      install.bat uses it instead of the network, then run.bat works.
    echo.
    goto :end
)
echo [run.bat] Python: %PY%

:have_python
REM ---- 4. A .venv still wins over a system Python (developers), but not
REM         over the bundled runtime. ------------------------------------
set "RUNPY=%PY%"
if /i "%PY%"=="%RUNTIME_PY%" goto :port_check
if exist ".venv\Scripts\python.exe" (
    set "RUNPY=.venv\Scripts\python.exe"
    echo [run.bat] using .venv
) else (
    echo [run.bat] no .venv; using %PY%
    echo [run.bat] to make one:  "%PY%" -m venv .venv
)

:port_check
REM ---- 5. Port 80 needs Administrator. Warn instead of failing silently.
net session >nul 2>&1
if errorlevel 1 (
    echo.
    echo [run.bat] Not running as Administrator: port 80 will be refused.
    echo [run.bat] Either right-click this file and choose
    echo [run.bat] "Run as administrator", or use another port:
    echo [run.bat]     run.bat --port 8080
    echo.
)

if not exist "web\dashboard.html" (
    echo [run.bat] web\dashboard.html is missing; extracting it from EVDCTRL.h...
    "%RUNPY%" extract_dashboard.py EVDCTRL.h web\dashboard.html
    echo.
)

echo [run.bat] starting: %RUNPY% -m enkelvoud %*
echo.
"%RUNPY%" -m enkelvoud %*
set "EXITCODE=%errorlevel%"

echo.
echo ================================================================
if not "%EXITCODE%"=="0" (
    echo  The server stopped with exit code %EXITCODE%.
    echo  The messages above say why.
) else (
    echo  The server stopped normally.
)
echo ================================================================

:end
echo.
echo Press any key to close this window...
pause >nul
exit /b 0

REM ==========================================================================
REM  Helpers. Not reached by falling through: :end exits above.
REM ==========================================================================

:find_system_python
REM Sets PY to a full path to python.exe (3.10+) or leaves it undefined.
REM sys.hexversion keeps the test free of parentheses and commas, so cmd's
REM block parser cannot trip over it inside a redirect.
set "PY="
set "PYPROBE=%TEMP%\enkelvoud_pyprobe.txt"
if not defined TEMP set "PYPROBE=%~dp0.pyprobe.txt"

py -3 -c "import sys;sys.hexversion>=0x030A0000 and print(sys.executable)" >"%PYPROBE%" 2>nul
if not errorlevel 1 call :read_pyprobe
if defined PY goto :find_system_python_done

python -c "import sys;sys.hexversion>=0x030A0000 and print(sys.executable)" >"%PYPROBE%" 2>nul
if not errorlevel 1 call :read_pyprobe
if defined PY goto :find_system_python_done

python3 -c "import sys;sys.hexversion>=0x030A0000 and print(sys.executable)" >"%PYPROBE%" 2>nul
if not errorlevel 1 call :read_pyprobe
if defined PY goto :find_system_python_done

REM Nothing on PATH. Look where the python.org installer puts it by default,
REM newest first (310 still has binary releases; 3.14+ is deliberately not
REM probed because requirements.txt is only tested up to 3.13).
for %%V in (313 312 311 310) do (
    if not defined PY (
        if exist "%LocalAppData%\Programs\Python\Python%%V\python.exe" set "PY=%LocalAppData%\Programs\Python\Python%%V\python.exe"
        if not defined PY if exist "%ProgramFiles%\Python%%V\python.exe" set "PY=%ProgramFiles%\Python%%V\python.exe"
        if not defined PY if exist "%ProgramFiles(x86)%\Python%%V\python.exe" set "PY=%ProgramFiles(x86)%\Python%%V\python.exe"
        if not defined PY if exist "C:\Python%%V\python.exe" set "PY=C:\Python%%V\python.exe"
    )
)

:find_system_python_done
if exist "%PYPROBE%" del "%PYPROBE%" >nul 2>&1
exit /b 0

:read_pyprobe
REM Reads the path the probe printed; rejects anything that is not a real file
REM (the Microsoft Store python.exe stub prints an error message instead).
set "PROBED="
set /p PROBED=<"%PYPROBE%" 2>nul
if not defined PROBED exit /b 0
if not exist "%PROBED%" exit /b 0
set "PY=%PROBED%"
exit /b 0
