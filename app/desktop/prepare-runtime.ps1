$ErrorActionPreference = 'Stop'
$Root = (Resolve-Path "$PSScriptRoot/../..").Path
$Runtime = Join-Path $Root 'runtime'
New-Item -ItemType Directory -Force "$Runtime/python", "$Runtime/installers" | Out-Null

# Match the official Python installed by setup-python; use its embeddable Windows distribution.
$PythonVersion = (& python -c 'import platform; print(platform.python_version())').Trim()
if ($LASTEXITCODE -ne 0) { throw 'Cannot determine Python version' }
$PythonZip = Join-Path $env:RUNNER_TEMP 'rpts-python.zip'
Invoke-WebRequest "https://www.python.org/ftp/python/$PythonVersion/python-$PythonVersion-embed-amd64.zip" -OutFile $PythonZip
Expand-Archive $PythonZip "$Runtime/python" -Force
$Pth = Get-ChildItem "$Runtime/python/python*._pth" | Select-Object -First 1
$PythonTag = (& python -c 'import sys; print(str(sys.version_info.major)+str(sys.version_info.minor))').Trim()
Set-Content $Pth.FullName "python$PythonTag.zip`n.`nLib`nLib/site-packages`nimport site" -Encoding ascii

Invoke-WebRequest 'https://dot.net/v1/dotnet-install.ps1' -OutFile "$env:RUNNER_TEMP/rpts-dotnet-install.ps1"
& "$env:RUNNER_TEMP/rpts-dotnet-install.ps1" -Channel 8.0 -Runtime windowsdesktop -Architecture x64 -InstallDir "$Runtime/dotnet" -NoPath
if ($LASTEXITCODE -ne 0) { throw 'Portable .NET installation failed' }
Invoke-WebRequest 'https://aka.ms/vs/17/release/vc_redist.x64.exe' -OutFile "$Runtime/installers/VC_redist.x64.exe"
& "$Runtime/python/python.exe" -c 'import sqlite3, ssl, ctypes, zipfile; print("Portable Python OK")'
if ($LASTEXITCODE -ne 0) { throw 'Portable Python self check failed' }
