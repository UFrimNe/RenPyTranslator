param(
    [switch]$SkipTests,
    [switch]$SkipRuntime,
    [switch]$NoInstaller,
    [string]$BuildId = '',
    [long]$BuildNumber = 0,
    [string]$Python = 'python',
    [string]$OutDir = ''
)
$ErrorActionPreference = 'Stop'
$Root = (Resolve-Path "$PSScriptRoot/../..").Path
if (-not $OutDir) { $OutDir = Join-Path $Root 'dist' }
$OutDir = [IO.Path]::GetFullPath($OutDir)
New-Item -ItemType Directory -Force $OutDir | Out-Null
Push-Location $Root
try {
    & $Python -c 'import sys; assert sys.version_info >= (3,11), "Python 3.11+ required"'
    if ($LASTEXITCODE -ne 0) { throw 'Python 3.11+ is required' }
    & dotnet --version
    if ($LASTEXITCODE -ne 0) { throw '.NET 8 SDK is required' }
    if (-not $BuildId) {
        $BuildId = '0000000000000000000000000000000000000000'
        if (Get-Command git -ErrorAction SilentlyContinue) {
            $GitId = (& git rev-parse HEAD 2>$null)
            if ($LASTEXITCODE -eq 0) { $BuildId = $GitId }
        }
    }
    if ($BuildNumber -eq 0) { $BuildNumber = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds() }
    if (-not $SkipTests) {
        & $Python app/tools/check_source.py
        if ($LASTEXITCODE -ne 0) { throw 'Regression tests failed' }
    }
    & "$Root/app/desktop/build.ps1"
    if ($LASTEXITCODE -ne 0) { throw 'CEF build failed' }
    & "$Root/app/launcher/build.cmd"
    if ($LASTEXITCODE -ne 0) { throw 'Native RPTS.exe build failed' }
    if (-not $SkipRuntime) {
        & "$Root/app/desktop/prepare-runtime.ps1" -Python $Python
    }
    & $Python app/tools/package_release.py --out $OutDir --build-id $BuildId --build-number $BuildNumber
    if ($LASTEXITCODE -ne 0) { throw 'Portable ZIP packaging failed' }
    $Version = (Get-Content app/VERSION.txt -Raw).Trim()
    $Portable = Join-Path $OutDir "RenPyTranslator_$($Version.Replace('.','_'))_win64.zip"
    & $Python app/tools/package_source.py --out $OutDir
    if ($LASTEXITCODE -ne 0) { throw 'Source ZIP packaging failed' }
    if (-not $NoInstaller) {
        $Iscc = Join-Path ${env:ProgramFiles(x86)} 'Inno Setup 6/ISCC.exe'
        if (-not (Test-Path $Iscc)) {
            $Command = Get-Command ISCC.exe -ErrorAction SilentlyContinue
            if ($Command) { $Iscc = $Command.Source }
        }
        if (-not (Test-Path $Iscc)) { throw 'Install Inno Setup 6 or use Build.bat -NoInstaller' }
        $Staging = Join-Path $Root 'build/installer-input'
        if (Test-Path $Staging) { Remove-Item $Staging -Recurse -Force }
        Expand-Archive $Portable $Staging
        & $Iscc "/DAppVersion=$Version" "/DPayloadDir=$Staging/RenPyTranslator" "/DArtifactDir=$OutDir" "$Root/app/installer/RPTS.iss"
        if ($LASTEXITCODE -ne 0) { throw 'Setup EXE build failed' }
    }
    & $Python app/tools/package_source.py --checksums-only --out $OutDir
    if ($LASTEXITCODE -ne 0) { throw 'Checksum generation failed' }
    Write-Host "Complete program and sources: $OutDir"
} finally { Pop-Location }
