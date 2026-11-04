param([string]$Python = "python")
$ErrorActionPreference = 'Stop'
$Root = (Resolve-Path "$PSScriptRoot/../..").Path
$Runtime = Join-Path $Root 'runtime'
$DownloadDir = Join-Path ([IO.Path]::GetTempPath()) ("rpts-runtime-" + [Guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Force $DownloadDir | Out-Null
try {
New-Item -ItemType Directory -Force "$Runtime/python", "$Runtime/installers" | Out-Null

# Match the official Python installed by setup-python; use its embeddable Windows distribution.
$PythonVersion = (& $Python -c 'import platform; print(platform.python_version())').Trim()
if ($LASTEXITCODE -ne 0) { throw 'Cannot determine Python version' }
$PythonZip = Join-Path $DownloadDir 'rpts-python.zip'
Invoke-WebRequest "https://www.python.org/ftp/python/$PythonVersion/python-$PythonVersion-embed-amd64.zip" -OutFile $PythonZip
Expand-Archive $PythonZip "$Runtime/python" -Force
$Pth = Get-ChildItem "$Runtime/python/python*._pth" | Select-Object -First 1
$PythonTag = (& $Python -c 'import sys; print(str(sys.version_info.major)+str(sys.version_info.minor))').Trim()
Set-Content $Pth.FullName "python$PythonTag.zip`n.`nLib`nLib/site-packages`nimport site" -Encoding ascii

Invoke-WebRequest 'https://dot.net/v1/dotnet-install.ps1' -OutFile "$DownloadDir/rpts-dotnet-install.ps1"
# The Windows Desktop ZIP contains the shared GUI framework, but not dotnet.exe/hostfxr.
& "$DownloadDir/rpts-dotnet-install.ps1" -Channel 8.0 -Runtime dotnet -Architecture x64 -InstallDir "$Runtime/dotnet" -NoPath
if ($LASTEXITCODE -ne 0) { throw 'Portable .NET host installation failed' }
& "$DownloadDir/rpts-dotnet-install.ps1" -Channel 8.0 -Runtime windowsdesktop -Architecture x64 -InstallDir "$Runtime/dotnet" -NoPath
if ($LASTEXITCODE -ne 0) { throw 'Portable .NET installation failed' }
& "$Runtime/dotnet/dotnet.exe" --list-runtimes
if ($LASTEXITCODE -ne 0) { throw 'Portable .NET self check failed' }
Invoke-WebRequest 'https://aka.ms/vs/17/release/vc_redist.x64.exe' -OutFile "$Runtime/installers/VC_redist.x64.exe"
& "$Runtime/python/python.exe" -c 'import sqlite3, ssl, ctypes, zipfile; print("Portable Python OK")'
if ($LASTEXITCODE -ne 0) { throw 'Portable Python self check failed' }

} finally { Remove-Item $DownloadDir -Recurse -Force -ErrorAction SilentlyContinue }
