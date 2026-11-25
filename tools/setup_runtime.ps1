<#
    tools\setup_runtime.ps1 - build a private, self-contained Python for Enkelvoud PC.

    Why this exists
    ---------------
    run.bat used to open with "No Python 3.10 or newer found" on any PC that had
    no Python installed, which is the normal state of a Windows box. Telling the
    user to go and install Python, tick a checkbox and come back is exactly the
    kind of step that makes a double-click launcher useless.

    So the launcher now brings its own Python. This script downloads the official
    Windows "embeddable" package from python.org (about 11 MB, no installer, no
    registry, no PATH changes), unpacks it into runtime\python, enables    site-packages in its ._pth file, bootstraps pip with get-pip.py, and installs
    requirements.txt into it.

    After that, run.bat uses runtime\python\python.exe and never looks at the
    system Python again. Nothing outside this folder is touched: no install, no
    HKCU/HKLM writes, no PATH edit. Deleting runtime\ undoes all of it.

    Offline machines: download python-3.13.15-embed-amd64.zip on any PC and drop
    it in vendor\ next to run.bat; this script uses that file instead of the
    network.

    Usage
    -----
        powershell -NoProfile -ExecutionPolicy Bypass -File tools\setup_runtime.ps1
        ... -PythonVersion 3.12.16     # pin a different Python
        ... -Force                     # wipe runtime\python and rebuild

    Exit code 0 means runtime\python\python.exe works and has the requirements.
#>
#requires -version 5

[CmdletBinding()]
param(
    # Embeddable release to fetch. $env:ENKELVOUD_PY_VERSION overrides the default.
    [string] $PythonVersion = $env:ENKELVOUD_PY_VERSION,
    # Wipe runtime\python and start over.
    [switch] $Force
)

$ErrorActionPreference = 'Stop'
$ProgressPreference    = 'SilentlyContinue'   # IWR's progress bar is very slow

$Root       = Split-Path -Parent $PSScriptRoot          # ...\EnkelvoudPC-main
$RuntimeDir = Join-Path $Root 'runtime'
$PyDir      = Join-Path $RuntimeDir 'python'
$PyExe      = Join-Path $PyDir 'python.exe'
$StampFile  = Join-Path $RuntimeDir 'runtime.json'
$ReqFile    = Join-Path $Root 'requirements.txt'
$VendorDir  = Join-Path $Root 'vendor'
$CacheDir   = Join-Path $RuntimeDir 'download'
$GetPipUrl  = 'https://bootstrap.pypa.io/get-pip.py'

# Newest first. python.org stops shipping binaries once a branch goes
# security-only (3.13.16 and later are source-only), so the first entry can
# disappear; the rest are fall-backs that still have embeddable zips.
$FallbackVersions = @(
    '3.13.15', '3.13.14', '3.13.13', '3.13.12', '3.13.11', '3.13.10',
    '3.12.16', '3.12.15', '3.12.10'
)

function Write-Step([string] $Text) { Write-Host "[setup] $Text" }
function Write-Note([string] $Text) { Write-Host "[setup] $Text" -ForegroundColor DarkGray }

# One line, at most 160 chars: exception messages carry multi-line stack-ish
# detail that is useless in a "nothing worked, here is what to do" message.
function Shorten([string] $Text) {
    $lines = @($Text -split "`r?`n" | Where-Object { $_.Trim() })
    $line = 'unknown error'
    if ($lines.Count -gt 0) { $line = $lines[0].Trim() }
    if ($line.Length -gt 160) { $line = $line.Substring(0, 160) + '...' }
    return $line
}

# SHA-256 in .NET, not Get-FileHash: that cmdlet lives in a module that is not
# always loadable (see the note where the stamp is written).
function Get-Sha256([string] $Path) {
    $sha = [System.Security.Cryptography.SHA256]::Create()
    $stream = [System.IO.File]::OpenRead($Path)
    try { $hash = $sha.ComputeHash($stream) }
    finally { $stream.Dispose(); $sha.Dispose() }
    return (($hash | ForEach-Object { $_.ToString('x2') }) -join '')
}

# --------------------------------------------------------------------------
# Download one URL to one path, trying every mechanism Windows offers, because
# a single one of them is regularly broken: PS 5.1 needs TLS 1.2 forced on,
# curl.exe is absent on Windows 8, IWR is blocked by some proxies, BITS is
# blocked by others.
# --------------------------------------------------------------------------
function Get-RemoteFile {
    param([string] $Url, [string] $Dest)

    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $Dest) | Out-Null
    $part = "$Dest.part"
    foreach ($f in @($part, $Dest)) {
        if (Test-Path -LiteralPath $f) { Remove-Item -LiteralPath $f -Force }
    }

    $problems = @()

    # 1. Invoke-WebRequest -- honours WinINET/WinHTTP proxy settings.
    try {
        Write-Step "downloading $Url"
        Invoke-WebRequest -Uri $Url -OutFile $part -UseBasicParsing -TimeoutSec 900
    } catch {
        $problems += "Invoke-WebRequest: $(Shorten $_.Exception.Message)"
    }

    # 2. curl.exe -- ships with Windows 10 1803+.
    if (-not (Test-Path -LiteralPath $part)) {
        $curl = (Get-Command curl.exe -ErrorAction SilentlyContinue).Source
        if ($curl) {
            try {
                Write-Step 'retrying with curl.exe'
                & $curl -sS -L --fail --retry 3 --retry-delay 2 --max-time 1800 `
                        -o $part $Url 2>$null
                if ($LASTEXITCODE -ne 0) { throw "curl.exe exited $LASTEXITCODE" }
            } catch {
                $problems += "curl.exe: $(Shorten $_.Exception.Message)"
                if (Test-Path -LiteralPath $part) { Remove-Item $part -Force }
            }
        }
    }

    # 3. .NET WebClient -- a different HTTP stack again (WinHTTP vs WinINET).
    if (-not (Test-Path -LiteralPath $part)) {
        try {
            Write-Step 'retrying with System.Net.WebClient'
            $wc = New-Object System.Net.WebClient
            $wc.Headers.Add('User-Agent', 'EnkelvoudPC-runtime-setup')
            $wc.DownloadFile($Url, $part)
            $wc.Dispose()
        } catch {
            $problems += "WebClient: $(Shorten $_.Exception.Message)"
            if (Test-Path -LiteralPath $part) { Remove-Item $part -Force }
        }
    }

    # 4. BITS -- the Windows Update service. Slow but survives flaky links.
    if (-not (Test-Path -LiteralPath $part)) {
        try {
            Write-Step 'retrying with BITS'
            Start-BitsTransfer -Source $Url -Destination $part -ErrorAction Stop
        } catch {
            $problems += "BITS: $(Shorten $_.Exception.Message)"
            if (Test-Path -LiteralPath $part) { Remove-Item $part -Force }
        }
    }

    if (-not (Test-Path -LiteralPath $part)) {
        throw ("download failed for $Url`n" + (($problems | ForEach-Object { "    $_" }) -join "`n"))
    }

    Move-Item -LiteralPath $part -Destination $Dest -Force
    Write-Note ("  got {0:N1} MB" -f ((Get-Item -LiteralPath $Dest).Length / 1MB))
    return $Dest
}

# --------------------------------------------------------------------------
function Expand-ZipFile {
    param([string] $Zip, [string] $Dest)

    Add-Type -AssemblyName System.IO.Compression.FileSystem -ErrorAction Stop
    # OpenRead first: a truncated or HTML error page fails here, not halfway
    # through the extraction into a half-populated python directory.
    $probe = [System.IO.Compression.ZipFile]::OpenRead($Zip)
    try {
        if ($probe.Entries.Count -lt 5) { throw "not a Python archive (only $($probe.Entries.Count) entries)" }
    } finally {
        $probe.Dispose()
    }

    if (Test-Path -LiteralPath $Dest) { Remove-Item -LiteralPath $Dest -Recurse -Force }
    New-Item -ItemType Directory -Force -Path $Dest | Out-Null
    [System.IO.Compression.ZipFile]::ExtractToDirectory($Zip, $Dest)
}

# --------------------------------------------------------------------------
# The embeddable distribution disables site.py through pythonXY._pth, which is
# also what keeps pip's site-packages from ever being imported. Uncommenting
# "import site" is the documented switch; then <runtime>\Lib\site-packages is
# on sys.path like in a normal Python.
#
# The same file is why `python -m enkelvoud` says "No module named enkelvoud":
# with a ._pth present, sys.path is *exactly* those entries, and -m no longer
# prepends the working directory. So the project folder is added as an entry.
# Entries resolve relative to this ._pth (not the cwd -- verified from C:\),
# and runtime\python is always two levels below the project root, so "..\.."
# keeps working when the whole folder is moved or copied.
# --------------------------------------------------------------------------
function Enable-SitePackages {
    param([string] $PythonDir)

    $pth = @(Get-ChildItem -LiteralPath $PythonDir -Filter 'python*._pth' -File)
    if ($pth.Count -eq 0) { throw "no python*._pth in $PythonDir -- is this really the embeddable package?" }

    $projectEntry = '..\..'   # = $Root, the folder holding run.bat and enkelvoud\
    $lines = @(Get-Content -LiteralPath $pth[0].FullName)
    $out = New-Object System.Collections.Generic.List[string]
    $seenSite = $false
    foreach ($line in $lines) {
        if ($line -match '^\s*#\s*import\s+site\s*$' -or $line -match '^\s*import\s+site\s*$') {
            if (-not $seenSite) { $out.Add('import site'); $seenSite = $true }
        } else {
            $out.Add($line)
        }
    }
    if (-not $seenSite) { $out.Add('import site') }
    if (-not ($out -contains $projectEntry)) { $out.Add($projectEntry) }
    Set-Content -LiteralPath $pth[0].FullName -Value $out -Encoding Ascii

    $sitePkgs = Join-Path $PythonDir 'Lib\site-packages'
    New-Item -ItemType Directory -Force -Path $sitePkgs | Out-Null
    return $sitePkgs
}

# --------------------------------------------------------------------------
function Invoke-Python {
    param([string] $PythonExe, [string[]] $Arguments)

    & $PythonExe @Arguments
    $code = $LASTEXITCODE
    if ($code -ne 0) {
        throw ("$PythonExe $($Arguments -join ' ') failed with exit code $code")
    }
}

# --------------------------------------------------------------------------
function Find-LocalArchive {
    param([string] $Arch)

    if (-not (Test-Path -LiteralPath $VendorDir)) { return $null }
    $zips = @(Get-ChildItem -LiteralPath $VendorDir -Filter '*.zip' -File |
              Where-Object { $_.Name -match 'embed' -and $_.Name -match [regex]::Escape($Arch) })
    if ($zips.Count -eq 0) {
        $zips = @(Get-ChildItem -LiteralPath $VendorDir -Filter '*embed*.zip' -File)
    }
    if ($zips.Count -eq 0) { return $null }
    # Frozen builds should win when several zips are lying around.
    $pinned = $zips | Where-Object { $PythonVersion -and $_.Name -like "*$PythonVersion*" }
    if ($pinned) { return $pinned[0].FullName }
    return $zips[0].FullName
}

# --------------------------------------------------------------------------
function Main {
    Write-Host "=================================================================="
    Write-Host " Enkelvoud PC - private Python runtime setup"
    Write-Host "=================================================================="
    Write-Host ""

    foreach ($dir in @($RuntimeDir, $CacheDir)) {
        New-Item -ItemType Directory -Force -Path $dir | Out-Null
    }

    # TLS 1.2 for Windows PowerShell 5.1, which still defaults to SSLv3/TLS1.0
    # on old builds. Harmless on PowerShell 7.
    try {
        $current = [Net.ServicePointManager]::SecurityProtocol
        [Net.ServicePointManager]::SecurityProtocol = $current -bor [Net.SecurityProtocolType]::Tls12
    } catch { }

    if (-not (Test-Path -LiteralPath $ReqFile)) {
        throw "requirements.txt not found in $Root -- are you running this from the project folder?"
    }

    $arch = if ($env:PROCESSOR_ARCHITECTURE -eq 'ARM64') { 'arm64' } else { 'amd64' }

    # ---- 1. Get python.exe, unless a working one is already in place. -----
    $reuse = (Test-Path -LiteralPath $PyExe) -and (-not $Force)
    if ($reuse) {
        Write-Step "reusing existing $PyDir"
        Enable-SitePackages -PythonDir $PyDir | Out-Null
    } else {
        if ($Force -and (Test-Path -LiteralPath $PyDir)) {
            Write-Step "removing $PyDir (-Force)"
            Remove-Item -LiteralPath $PyDir -Recurse -Force
        }

        $archive = Find-LocalArchive -Arch $arch
        if ($archive) {
            Write-Step "using local archive $archive"
        } else {
            $versions = @()
            foreach ($candidate in (@($PythonVersion) + $FallbackVersions)) {
                if ($candidate -and -not ($versions -contains $candidate)) { $versions += $candidate }
            }

            $failed = @()
            $firstFailure = $null
            foreach ($v in $versions) {
                $url = "https://www.python.org/ftp/python/$v/python-$v-embed-$arch.zip"
                $dest = Join-Path $CacheDir "python-$v-embed-$arch.zip"
                try {
                    $archive = Get-RemoteFile -Url $url -Dest $dest
                    break
                } catch {
                    $failed += $v
                    if (-not $firstFailure) { $firstFailure = $_.Exception.Message }
                    Write-Note "  Python ${v}: download failed, trying the next version"
                }
            }
            if (-not $archive) {
                $detail = (($firstFailure -split "`r?`n") | ForEach-Object { "    $_" }) -join "`n"
                $hint  = "https://www.python.org/ftp/python/$($FallbackVersions[0])/python-$($FallbackVersions[0])-embed-amd64.zip"
                $msg = "could not download the Python embeddable package (tried $($failed -join ', ')).`n" +
                       "`n  The first attempt ($($failed[0])) said:`n$detail`n" +
                       "`n  If this PC has no internet, download this file on another PC:`n" +
                       "    $hint`n" +
                       "  and put it in the vendor\ folder next to run.bat, then run:`n" +
                       "    install.bat`n" +
                       "`n  Behind a proxy? Set HTTPS_PROXY (or HTTP_PROXY) before running this."
                throw $msg
            }
        }

        $staging = Join-Path $RuntimeDir '.extract'
        Write-Step "extracting $(Split-Path -Leaf $archive) into runtime\python"
        Expand-ZipFile -Zip $archive -Dest $staging

        if (-not (Test-Path -LiteralPath (Join-Path $staging 'python.exe'))) {
            Remove-Item -LiteralPath $staging -Recurse -Force -ErrorAction SilentlyContinue
            throw "the archive did not contain python.exe -- wrong file, or a partial download"
        }
        if (Test-Path -LiteralPath $PyDir) { Remove-Item -LiteralPath $PyDir -Recurse -Force }
        Move-Item -LiteralPath $staging -Destination $PyDir -Force

        $sitePkgs = Enable-SitePackages -PythonDir $PyDir
        Write-Note "  enabled site-packages: $sitePkgs"
    }

    if (-not (Test-Path -LiteralPath $PyExe)) { throw "python.exe is missing from $PyDir" }

    $ver = (& $PyExe -c "import sys;print('.'.join(map(str,sys.version_info[:3])))").Trim()
    Write-Step "python.exe reports $ver"

    # ---- 2. pip ----------------------------------------------------------
    $havePip = $false
    try {
        & $PyExe -m pip --version *> $null
        $havePip = ($LASTEXITCODE -eq 0)
    } catch { $havePip = $false }

    if (-not $havePip) {
        Write-Step 'bootstrapping pip (get-pip.py, one time)'
        $getPip = Join-Path $CacheDir 'get-pip.py'
        $localGetPip = Join-Path $VendorDir 'get-pip.py'
        if (Test-Path -LiteralPath $localGetPip) {
            $getPip = $localGetPip
            Write-Note "  using $localGetPip"
        } elseif (-not (Test-Path -LiteralPath $getPip)) {
            $getPip = Get-RemoteFile -Url $GetPipUrl -Dest $getPip
        }
        Invoke-Python -PythonExe $PyExe -Arguments @($getPip, '--no-input', '--no-warn-script-location', '--disable-pip-version-check')
    } else {
        Write-Note '  pip already present'
    }

    # ---- 3. requirements -------------------------------------------------
    Write-Step 'installing requirements.txt (Flask, numpy, scipy, pyaudiowpatch, ...)'
    Write-Note '  this is the slow part: numpy and scipy are ~60 MB of wheels'
    $env:PIP_DISABLE_PIP_VERSION_CHECK = '1'

    $pipArgs = @(
        '-m', 'pip', 'install',
        '--no-input', '--prefer-binary', '--no-warn-script-location'
    )
    # Fully offline install: `pip download -r requirements.txt -d vendor\wheels`
    # on a connected PC, copy vendor\wheels over, and pip never touches the net.
    $wheelDir = Join-Path $VendorDir 'wheels'
    $wheels = @()
    if (Test-Path -LiteralPath $wheelDir) {
        $wheels = @(Get-ChildItem -LiteralPath $wheelDir -Filter '*.whl' -File)
    }
    if ($wheels.Count -gt 0) {
        Write-Note "  offline: using $($wheels.Count) wheels from vendor\wheels"
        $pipArgs += @('--no-index', '--find-links', $wheelDir)
    }
    $pipArgs += @('-r', $ReqFile)
    Invoke-Python -PythonExe $PyExe -Arguments $pipArgs

    # ---- 4. Prove it actually imports. A "successful" pip log is not proof:
    # a missing wheel leaves pip happy and the app dead at the first import.
    Write-Step 'checking the installed packages'
    $imports = 'flask, zeroconf, numpy, scipy, psutil, requests, pyaudiowpatch'
    Invoke-Python -PythonExe $PyExe -Arguments @(
        '-c', "import $imports; print('imports OK')"
    )

    $opusOk = $false
    try {
        Push-Location $Root
        & $PyExe (Join-Path $Root 'opus_runtime.py') --check *> $null
        $opusOk = ($LASTEXITCODE -eq 0)
    } catch { $opusOk = $false } finally { Pop-Location }
    if ($opusOk) { Write-Note '  Opus encoder: OK' }
    else { Write-Host '[setup] WARNING: the Opus encoder check failed; audio will not encode. Run: runtime\python\python.exe opus_runtime.py --install' -ForegroundColor Yellow }

    # ---- 5. Marker: run.bat only trusts a runtime that reached this point.
    # Written with .NET only: no Get-FileHash / ConvertTo-Json / Measure-Object.
    # Those are Microsoft.PowerShell.Utility cmdlets, and on a locked-down PC
    # (or when a PS7 module path is inherited) autoloading them can fail even
    # though the runtime itself is fine -- the install must not die there.
    $stamp = "{`r`n" +
             "  `"python`": `"$ver`",`r`n" +
             "  `"arch`": `"$arch`",`r`n" +
             "  `"installed_utc`": `"$((Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ'))`",`r`n" +
             "  `"requirements_sha256`": `"$(Get-Sha256 $ReqFile)`",`r`n" +
             "  `"opus`": $($opusOk.ToString().ToLower()),`r`n" +
             "  `"python_exe`": `"runtime\\python\\python.exe`"`r`n" +
             "}`r`n"
    [System.IO.File]::WriteAllText($StampFile, $stamp, (New-Object System.Text.UTF8Encoding($false)))

    $mb = 0
    try {
        $bytes = 0
        foreach ($f in @(Get-ChildItem -LiteralPath $RuntimeDir -Recurse -File -ErrorAction SilentlyContinue)) {
            $bytes += $f.Length
        }
        $mb = [math]::Round($bytes / 1MB, 1)
    } catch { }

    Write-Host ""
    Write-Host "=================================================================="
    Write-Host " Done: Python $ver is in runtime\python ($mb MB)"
    Write-Host " Nothing was installed system-wide, no PATH or registry changes."
    Write-Host " Start the server with:  run.bat"
    Write-Host "=================================================================="
    Write-Host ""
}

try {
    Main
    exit 0
} catch {
    Write-Host ""
    Write-Host "[setup] FAILED" -ForegroundColor Red
    Write-Host "[setup] $($_.Exception.Message)" -ForegroundColor Red
    Write-Host ""
    Write-Host "[setup] Nothing is left half-installed: run install.bat again to retry,"
    Write-Host "[setup] or 'install.bat -Force' to throw runtime\python away first."
    exit 1
}
