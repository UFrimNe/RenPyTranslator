$ErrorActionPreference = 'Stop'
$Version = (Get-Content "$PSScriptRoot/../VERSION.txt" -Raw).Trim()
$Root = (Resolve-Path "$PSScriptRoot/../..").Path
$Destination = Join-Path $env:RUNNER_TEMP 'RPTS install test unicode Тест'
New-Item -ItemType Directory -Force "$Destination/data/settings", "$Destination/runtime/local_ai" | Out-Null
Set-Content "$Destination/data/settings/keep.txt" 'user-settings'
Set-Content "$Destination/runtime/local_ai/keep.txt" 'user-models'
$Setup = Join-Path $Root "dist/RenPyTranslator_$($Version)_Setup_win64.exe"
$Result = Start-Process $Setup -ArgumentList @('/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', "/DIR=`"$Destination`"", "/LOG=`"$env:RUNNER_TEMP/rpts-setup.log`"") -Wait -PassThru
if ($Result.ExitCode -ne 0) { throw "Setup failed: $($Result.ExitCode)" }
foreach ($File in @('RPTS.exe', 'RPTS.bat', 'RPTS-Console.bat', 'RPTS-Debug.bat', 'runtime/python/python.exe', 'runtime/dotnet/dotnet.exe', 'app/desktop/bin/RPTS.Desktop.exe', 'data/settings/keep.txt', 'runtime/local_ai/keep.txt')) {
    if (-not (Test-Path "$Destination/$File")) { throw "Missing installed file: $File" }
}
$Result = Start-Process "$Destination/RPTS.exe" -ArgumentList '--self-check' -Wait -PassThru
if ($Result.ExitCode -ne 0) { throw 'Installed EXE failed' }
& "$Destination/runtime/python/python.exe" "$Destination/app/tools/launch_desktop.py" --report-only
if ($LASTEXITCODE -ne 0) { throw 'Installed diagnostic entry point failed' }
& "$Destination/runtime/python/python.exe" -c "import pathlib,json,hashlib,sys; r=pathlib.Path(sys.argv[1]); m=json.loads((r/'app/release_manifest.json').read_text(encoding='utf-8')); bad=[x['path'] for x in m['files'] if not (r/x['path']).is_file() or hashlib.sha256((r/x['path']).read_bytes()).hexdigest()!=x['sha256']]; assert not bad,bad; print('All installed payload hashes verified')" $Destination
if ($LASTEXITCODE -ne 0) { throw 'Installed files differ from release manifest' }
$Result = Start-Process "$Destination/unins000.exe" -ArgumentList '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART' -Wait -PassThru
if ($Result.ExitCode -ne 0) { throw 'Uninstall failed' }
foreach ($File in @('data/settings/keep.txt', 'runtime/local_ai/keep.txt')) {
    if (-not (Test-Path "$Destination/$File")) { throw "User data was removed: $File" }
}
Write-Host 'Installer/uninstaller, Unicode path and user data preservation passed'
