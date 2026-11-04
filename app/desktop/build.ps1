$ErrorActionPreference = 'Stop'
# Requires .NET 8 SDK; CefSharp/CEF packages are restored from NuGet.
dotnet publish "$PSScriptRoot/src/RptsDesktop.csproj" -c Release -r win-x64 --self-contained false -o "$PSScriptRoot/bin"
if ($LASTEXITCODE -ne 0) { throw 'CEF build failed' }

dotnet publish "$PSScriptRoot/picker/FolderPicker.csproj" -c Release --self-contained false -o "$PSScriptRoot/picker-publish"
if ($LASTEXITCODE -ne 0) { throw "Folder picker build failed" }
Copy-Item "$PSScriptRoot/picker-publish/RPTS.FolderPicker.*" "$PSScriptRoot/bin/" -Force
