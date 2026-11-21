$ErrorActionPreference = "Stop"

$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $ProjectRoot
$env:UV_CACHE_DIR = Join-Path $env:LOCALAPPDATA "pikmin-yolo-uv-cache"

function Find-Uv {
    $UserUv = Join-Path $env:USERPROFILE ".local\bin\uv.exe"
    if (Test-Path -LiteralPath $UserUv) { return $UserUv }

    $Command = Get-Command uv -ErrorAction SilentlyContinue
    if ($Command) { return $Command.Source }

    $Winget = Get-Command winget -ErrorAction SilentlyContinue
    if (-not $Winget) {
        throw "uv was not found. Install it from https://docs.astral.sh/uv/."
    }
    Write-Host "Installing uv..." -ForegroundColor Cyan
    & $Winget.Source install --id astral-sh.uv --exact --accept-package-agreements --accept-source-agreements
    if ($LASTEXITCODE -ne 0) { throw "uv installation failed." }

    $Command = Get-Command uv -ErrorAction SilentlyContinue
    if ($Command) { return $Command.Source }
    if (Test-Path -LiteralPath $UserUv) { return $UserUv }
    throw "uv was installed but uv.exe could not be located. Reopen the terminal and retry."
}

function Install-And-Find-UvPython312([string]$Uv) {
    Write-Host "Ensuring uv-managed Python 3.12 is installed..." -ForegroundColor Cyan
    & $Uv python install 3.12
    if ($LASTEXITCODE -ne 0) { throw "uv could not install Python 3.12." }

    $ManagedRoot = (& $Uv python dir | Select-Object -Last 1).Trim()
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $ManagedRoot)) {
        throw "The uv-managed Python directory could not be located."
    }
    $Python = Get-ChildItem -LiteralPath $ManagedRoot -Directory -Filter "cpython-3.12*-windows-*" |
        Sort-Object Name -Descending |
        ForEach-Object { Join-Path $_.FullName "python.exe" } |
        Where-Object { Test-Path -LiteralPath $_ } |
        Select-Object -First 1
    if (-not $Python) { throw "uv-managed Python 3.12 was installed but python.exe was not found." }
    return $Python
}

function Sync-UvProject([string]$Uv, [string]$Python) {
    # Python 3.12 asks Windows WMI for platform details. A broken WMI service can
    # otherwise leave uv waiting forever, so hide only uv's private copy during sync.
    $PythonRoot = Split-Path -Parent $Python
    $StaleBackup = Get-ChildItem -LiteralPath $PythonRoot -File -Recurse -Filter "_wmi.pyd.pikmin_uv_setup_disabled" |
        Select-Object -First 1
    if ($StaleBackup) {
        $Original = $StaleBackup.FullName.Substring(0, $StaleBackup.FullName.Length - ".pikmin_uv_setup_disabled".Length)
        if (-not (Test-Path -LiteralPath $Original)) {
            Move-Item -LiteralPath $StaleBackup.FullName -Destination $Original
        }
    }

    $WmiModule = Get-ChildItem -LiteralPath $PythonRoot -File -Recurse -Filter "_wmi.pyd" |
        Select-Object -First 1
    $WmiBackup = $null
    if ($WmiModule) {
        $WmiBackup = $WmiModule.FullName + ".pikmin_uv_setup_disabled"
        Move-Item -LiteralPath $WmiModule.FullName -Destination $WmiBackup
    }
    try {
        & $Uv sync --python $Python --locked --link-mode copy
        if ($LASTEXITCODE -ne 0) { throw "uv sync failed." }
    }
    finally {
        if ($WmiBackup -and (Test-Path -LiteralPath $WmiBackup)) {
            Move-Item -LiteralPath $WmiBackup -Destination $WmiModule.FullName
        }
    }
}

function Find-ProjectScrcpy {
    $Executable = Join-Path $ProjectRoot "vendor\scrcpy\scrcpy.exe"
    if (Test-Path -LiteralPath $Executable) { return $Executable }
    return $null
}

function Install-ProjectScrcpy {
    $VendorDirectory = Join-Path $ProjectRoot "vendor"
    $InstallDirectory = Join-Path $VendorDirectory "scrcpy"
    New-Item -ItemType Directory -Path $VendorDirectory -Force | Out-Null
    if (Test-Path -LiteralPath $InstallDirectory) {
        throw "vendor\scrcpy exists but is incomplete. Remove that incomplete directory and retry."
    }

    Write-Host "Downloading the official scrcpy Windows x64 package..." -ForegroundColor Cyan
    $Headers = @{ "User-Agent" = "BloomMushroomFinder-UV-Setup" }
    $Release = Invoke-RestMethod -Uri "https://api.github.com/repos/Genymobile/scrcpy/releases/latest" -Headers $Headers
    $Asset = $Release.assets | Where-Object { $_.name -match '^scrcpy-win64-v[0-9.]+\.zip$' } | Select-Object -First 1
    if (-not $Asset) { throw "No Windows x64 scrcpy ZIP was found in the latest release." }

    $DownloadPath = Join-Path $VendorDirectory $Asset.name
    $ExtractDirectory = Join-Path $VendorDirectory ("scrcpy_extract_" + (Get-Date -Format "yyyyMMdd_HHmmss"))
    try {
        Invoke-WebRequest -Uri $Asset.browser_download_url -Headers $Headers -OutFile $DownloadPath -UseBasicParsing
        if ($Asset.digest -and $Asset.digest -match '^sha256:(.+)$') {
            $ExpectedHash = $Matches[1].ToLowerInvariant()
            $ActualHash = (Get-FileHash -LiteralPath $DownloadPath -Algorithm SHA256).Hash.ToLowerInvariant()
            if ($ActualHash -ne $ExpectedHash) { throw "scrcpy SHA-256 verification failed." }
        }
        Expand-Archive -LiteralPath $DownloadPath -DestinationPath $ExtractDirectory
        $Scrcpy = Get-ChildItem -LiteralPath $ExtractDirectory -Filter "scrcpy.exe" -Recurse | Select-Object -First 1
        if (-not $Scrcpy) { throw "scrcpy.exe was not found in the downloaded archive." }
        Move-Item -LiteralPath $Scrcpy.Directory.FullName -Destination $InstallDirectory
    }
    finally {
        if (Test-Path -LiteralPath $DownloadPath) { Remove-Item -LiteralPath $DownloadPath -Force }
        if (Test-Path -LiteralPath $ExtractDirectory) { Remove-Item -LiteralPath $ExtractDirectory -Recurse -Force }
    }
    return (Join-Path $InstallDirectory "scrcpy.exe")
}

$Uv = Find-Uv
Write-Host "uv: $Uv" -ForegroundColor Green
Write-Host "Synchronizing the Python 3.12 uv project environment..." -ForegroundColor Cyan
if (-not (Test-Path -LiteralPath (Join-Path $ProjectRoot "uv.lock"))) {
    throw "uv.lock is missing. Restore it before configuring the environment."
}
$ManagedPython = Install-And-Find-UvPython312 $Uv
Sync-UvProject $Uv $ManagedPython

$Scrcpy = Find-ProjectScrcpy
if (-not $Scrcpy) { $Scrcpy = Install-ProjectScrcpy }
if (-not (Test-Path -LiteralPath (Join-Path (Split-Path $Scrcpy) "adb.exe")) -or
    -not (Test-Path -LiteralPath (Join-Path (Split-Path $Scrcpy) "scrcpy-server"))) {
    throw "The project scrcpy directory is missing adb.exe or scrcpy-server."
}

$Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
$PreviousPythonPath = $env:PYTHONPATH
$env:PYTHONPATH = Join-Path $ProjectRoot "scripts\uv_python_compat"
try {
    & $Python -c "import av, cv2, fastapi, PIL, torch, uvicorn, yaml; print(f'Python API, training, and phone dependencies: OK (torch {torch.__version__})')"
    if ($LASTEXITCODE -ne 0) { throw "Python dependency verification failed." }
}
finally {
    $env:PYTHONPATH = $PreviousPythonPath
}

Write-Host "Environment ready. scrcpy: $Scrcpy" -ForegroundColor Green

$Frontend = Join-Path $ProjectRoot "frontend"
$PackageJson = Join-Path $Frontend "package.json"
if (-not (Test-Path -LiteralPath $PackageJson)) {
    throw "Electron frontend package.json was not found at $PackageJson."
}
$Npm = Get-Command npm.cmd -ErrorAction SilentlyContinue
if (-not $Npm) {
    throw "Node.js/npm was not found. Install Node.js 18 or newer."
}
Write-Host "Installing Electron / React frontend dependencies..." -ForegroundColor Cyan
Push-Location $Frontend
try {
    if (Test-Path -LiteralPath (Join-Path $Frontend "package-lock.json")) {
        & $Npm.Source ci --no-audit --no-fund
    }
    else {
        & $Npm.Source install --no-audit --no-fund
    }
    if ($LASTEXITCODE -ne 0) { throw "npm frontend installation failed." }
}
finally {
    Pop-Location
}

Write-Host "Double-click 啟動BloomMushroomFinder控制中心.bat to open the Electron desktop UI." -ForegroundColor Green
