[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"

$repo = Split-Path -Parent $PSScriptRoot
$androidProject = $repo
$signingDir = Join-Path $repo "signing"
$signingProperties = Join-Path $signingDir "signing.properties"
$keystore = Join-Path $signingDir "locwarp-release.keystore"
$localProperties = Join-Path $repo "local.properties"
$configuredSdk = $null
if (Test-Path -LiteralPath $localProperties) {
    $sdkLine = Get-Content -LiteralPath $localProperties |
        Where-Object { $_ -match '^sdk\.dir=' } |
        Select-Object -First 1
    if ($sdkLine) {
        $configuredSdk = ($sdkLine -split '=', 2)[1].Trim()
        $configuredSdk = $configuredSdk.Replace('\:', ':').Replace('\\', '\')
    }
}
$sdkCandidates = @(
    $configuredSdk,
    (Join-Path $repo ".android-sdk"),
    (Join-Path (Split-Path -Parent $repo) ".android-sdk")
)
$androidSdk = $sdkCandidates |
    Where-Object { Test-Path -LiteralPath $_ } |
    Select-Object -First 1
if (-not $androidSdk) {
    throw "Android SDK was not found. Expected .android-sdk in the repo root or parent workspace."
}
$buildTools = Join-Path $androidSdk "build-tools\35.0.0"
$aapt = Join-Path $buildTools "aapt.exe"
$apksignerJar = Join-Path $buildTools "lib\apksigner.jar"
$expectedPackage = "com.locwarp.mock"
$expectedCertificate = "0832b67396143e7bd420dce24aa172143e38512c3beb93ffeedf9fb3a19585ec"

if (-not (Test-Path -LiteralPath $signingProperties)) {
    throw "Missing $signingProperties. See signing/README.md."
}
if (-not (Test-Path -LiteralPath $keystore)) {
    throw "Missing $keystore. See signing/README.md."
}
if (-not (Test-Path -LiteralPath $aapt) -or -not (Test-Path -LiteralPath $apksignerJar)) {
    throw "Android build tools 35.0.0 are required under $androidSdk."
}

$gradle = Join-Path $repo "gradlew.bat"
if (-not (Test-Path -LiteralPath $gradle)) {
    throw "Gradle Wrapper was not found at $gradle."
}

$jdkSearchRoots = @(
    (Join-Path $repo ".tmp\jdk17"),
    (Join-Path (Split-Path -Parent $repo) ".tmp\jdk17"),
    (Join-Path (Split-Path -Parent $androidSdk) ".tmp\jdk17")
)
$jdk17 = $env:JAVA_HOME
if (-not $jdk17 -or -not (Test-Path (Join-Path $jdk17 "bin\java.exe"))) {
    $jdk17 = $jdkSearchRoots |
        Where-Object { $_ } |
        Select-Object -Unique |
        ForEach-Object {
            Get-ChildItem $_ -Directory -ErrorAction SilentlyContinue
        } |
        Where-Object { Test-Path (Join-Path $_.FullName "bin\java.exe") } |
        Select-Object -First 1 -ExpandProperty FullName
}
if (-not $jdk17) {
    $jdk17 = Get-ChildItem "C:\Program Files\Java","C:\Program Files\Eclipse Adoptium" `
        -Directory -Recurse -ErrorAction SilentlyContinue |
        Where-Object {
            $_.Name -match "17" -and
            (Test-Path (Join-Path $_.FullName "bin\java.exe"))
        } |
        Select-Object -First 1 -ExpandProperty FullName
}
if (-not $jdk17) {
    throw "JDK 17 was not found. Install JDK 17 before building the Android release."
}

$previousJavaHome = $env:JAVA_HOME
$previousPath = $env:Path
$env:JAVA_HOME = $jdk17
$env:Path = "$(Join-Path $jdk17 'bin');$previousPath"

Push-Location $androidProject
try {
    & $gradle :app:testReleaseUnitTest :app:lintRelease :app:assembleRelease --no-daemon
    if ($LASTEXITCODE -ne 0) {
        throw "Android release build failed."
    }
} finally {
    Pop-Location
    $env:JAVA_HOME = $previousJavaHome
    $env:Path = $previousPath
}

$builtApk = Join-Path $androidProject "app\build\outputs\apk\release\app-release.apk"
if (-not (Test-Path -LiteralPath $builtApk)) {
    throw "Signed release APK was not produced at $builtApk."
}

$badging = & $aapt dump badging $builtApk
if ($LASTEXITCODE -ne 0) {
    throw "Unable to inspect APK metadata."
}
$packageLine = $badging | Select-Object -First 1
$packageMatch = [regex]::Match(
    $packageLine,
    "name='([^']+)'.*versionCode='([^']+)'.*versionName='([^']+)'"
)
if (-not $packageMatch.Success) {
    throw "Unable to parse APK package/version metadata."
}

$packageName = $packageMatch.Groups[1].Value
$versionCode = $packageMatch.Groups[2].Value
$versionName = $packageMatch.Groups[3].Value
if ($packageName -ne $expectedPackage) {
    throw "Unexpected APK package: $packageName."
}

$java = Join-Path $jdk17 "bin\java.exe"
$certificateOutput = & $java -jar $apksignerJar verify --print-certs $builtApk
if ($LASTEXITCODE -ne 0) {
    throw "APK signature verification failed."
}
$certificateLine = $certificateOutput |
    Select-String "certificate SHA-256 digest:" |
    Select-Object -First 1
if (-not $certificateLine) {
    throw "Unable to read the APK signing certificate."
}
$actualCertificate = (
    $certificateLine.Line -split "certificate SHA-256 digest:", 2
)[1].Trim().ToLowerInvariant()
if ($actualCertificate -ne $expectedCertificate) {
    throw "Wrong APK signing certificate: $actualCertificate."
}

$releaseDir = Join-Path $repo "release-assets"
New-Item -ItemType Directory -Force -Path $releaseDir | Out-Null
$target = Join-Path $releaseDir "BloomPilot-Android-$versionName.apk"
Copy-Item -LiteralPath $builtApk -Destination $target -Force

$hash = Get-FileHash -Algorithm SHA256 -LiteralPath $target
[PSCustomObject]@{
    Apk = $target
    Package = $packageName
    VersionName = $versionName
    VersionCode = $versionCode
    CertificateSha256 = $actualCertificate
    FileSha256 = $hash.Hash
}
