$ErrorActionPreference = 'Stop'
$Root = (Resolve-Path "$PSScriptRoot/../..").Path
$Version = (Get-Content "$Root/app/VERSION.txt" -Raw).Trim()
$Payload = Join-Path $Root 'build/installer-input/RenPyTranslator'
$BaselineDir = Join-Path $Root 'build/compression-baseline'
New-Item -ItemType Directory -Force $BaselineDir | Out-Null
$Iscc = Join-Path ([Environment]::GetFolderPath('ProgramFilesX86')) 'Inno Setup 6/ISCC.exe'
& $Iscc "/DAppVersion=$Version" "/DCompressionPreset=lzma2/fast" "/DPayloadDir=$Payload" "/DArtifactDir=$BaselineDir" "$Root/app/installer/RPTS.iss"
if ($LASTEXITCODE -ne 0) { throw 'Baseline compression build failed' }
$Name = "RenPyTranslator_$($Version)_Setup_win64.exe"
$FastBytes = (Get-Item "$BaselineDir/$Name").Length
$OptimizedBytes = (Get-Item "$Root/dist/$Name").Length
if ($OptimizedBytes -ge $FastBytes) { throw 'Strong compression did not reduce the installer; refusing release' }
$Inventory = @(Get-ChildItem $Payload -Recurse -File | Sort-Object Length -Descending | Select-Object -First 20 | ForEach-Object {
    @{path=[IO.Path]::GetRelativePath($Payload,$_.FullName);bytes=$_.Length}
})
$Report = @{
    version=$Version
    old_release_11_28_bytes=263660450
    same_payload_fast_bytes=$FastBytes
    optimized_bytes=$OptimizedBytes
    saved_bytes=$FastBytes-$OptimizedBytes
    saved_percent=[Math]::Round(100*($FastBytes-$OptimizedBytes)/$FastBytes,2)
    compression='lzma2/ultra64'
    largest_payload_files=$Inventory
}
$Report | ConvertTo-Json -Depth 5 | Set-Content "$Root/dist/installer-size-report.json" -Encoding utf8
Write-Host ($Report | ConvertTo-Json -Depth 5 -Compress)
& python "$Root/app/tools/package_source.py" --checksums-only --out "$Root/dist"
if ($LASTEXITCODE -ne 0) { throw 'Checksum refresh failed' }
