param([string[]]$RetireTags = @())
$ErrorActionPreference = 'Stop'
if (-not $env:GH_TOKEN -or -not $env:GH_REPO -or -not $env:GITHUB_SHA) { throw 'Publishing requires GitHub Actions credentials and commit metadata' }
if ($env:GITHUB_REF -ne 'refs/heads/main') { throw 'Public releases are published only from main' }
$Root = (Resolve-Path "$PSScriptRoot/../..").Path
Push-Location $Root
try {
    $Current = gh api "repos/$env:GH_REPO/git/ref/heads/main" --jq '.object.sha'
    if ($LASTEXITCODE -ne 0) { throw 'Cannot read main head' }
    if ($Current -ne $env:GITHUB_SHA) { Write-Host 'A newer main commit exists; keeping build artifact only'; exit 0 }
    $Version = (Get-Content app/VERSION.txt -Raw).Trim()
    $Tag = "v$Version"
    $Stem = "RenPyTranslator_$($Version.Replace('.','_'))"
    $Assets = @("dist/$($Stem)_win64.zip", "dist/RenPyTranslator_$($Version)_Setup_win64.exe", "dist/$($Stem)_source.zip", 'dist/update.json', 'dist/SHA256SUMS.txt')
    foreach ($Asset in $Assets) { if (-not (Test-Path $Asset)) { throw "Missing release asset: $Asset" } }
    $ExistingText = gh api "repos/$env:GH_REPO/releases/tags/$Tag" 2>$null
    $Exists = $LASTEXITCODE -eq 0
    if ($Exists) { $Existing = $ExistingText | ConvertFrom-Json }
    if ($Exists -and -not $Existing.draft) {
        Write-Host "$Tag is already published and remains unchanged. Bump app/VERSION.txt for a new release."
    } else {
        $Notes = "docs/releases/$Tag.md"
        if (-not (Test-Path $Notes)) { throw "Write release notes first: $Notes" }
        if (-not $Exists) {
            gh release create $Tag --target $env:GITHUB_SHA --draft --title "RenPyTranslator $Version" --notes-file $Notes
            if ($LASTEXITCODE -ne 0) { throw 'Cannot create release draft' }
        } else {
            gh release edit $Tag --target $env:GITHUB_SHA --title "RenPyTranslator $Version" --notes-file $Notes
            if ($LASTEXITCODE -ne 0) { throw 'Cannot refresh release draft' }
            gh api "repos/$env:GH_REPO/git/ref/tags/$Tag" --silent 2>$null
            if ($LASTEXITCODE -eq 0) {
                gh api "repos/$env:GH_REPO/git/refs/tags/$Tag" --method PATCH -f sha=$env:GITHUB_SHA -F force=true
                if ($LASTEXITCODE -ne 0) { throw 'Cannot align draft tag with build commit' }
            }
        }
        gh release upload $Tag @Assets --clobber
        if ($LASTEXITCODE -ne 0) { throw 'Cannot upload all release files' }
        $Published = (gh api "repos/$env:GH_REPO/releases/tags/$Tag") | ConvertFrom-Json
        if ($LASTEXITCODE -ne 0) { throw 'Cannot verify uploaded assets' }
        foreach ($Asset in $Assets) {
            $Local = Get-Item $Asset
            $Remote = $Published.assets | Where-Object { $_.name -eq $Local.Name }
            $Hash = 'sha256:' + (Get-FileHash $Local.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
            if (-not $Remote -or $Remote.size -ne $Local.Length -or ($Remote.digest -and $Remote.digest -ne $Hash)) {
                throw "Uploaded file failed verification: $Asset"
            }
        }
        gh release edit $Tag --draft=false --latest
        if ($LASTEXITCODE -ne 0) { throw 'Cannot publish verified release' }
    }
    # One-time migration: remove only the observed technical tags, after the replacement is published.
    foreach ($Old in $RetireTags) {
        if ($Old -notmatch '^dev-(latest|[0-9]+)$') { throw "Refusing unexpected legacy tag: $Old" }
        gh release view $Old 2>$null | Out-Null
        if ($LASTEXITCODE -eq 0) {
            gh release delete $Old --cleanup-tag --yes
            if ($LASTEXITCODE -ne 0) { throw "Cannot retire legacy release: $Old" }
        } else {
            gh api "repos/$env:GH_REPO/git/ref/tags/$Old" --silent 2>$null
            if ($LASTEXITCODE -eq 0) {
                gh api "repos/$env:GH_REPO/git/refs/tags/$Old" --method DELETE
                if ($LASTEXITCODE -ne 0) { throw "Cannot retire legacy tag: $Old" }
            }
        }
    }
    $global:LASTEXITCODE = 0
} finally { Pop-Location }
