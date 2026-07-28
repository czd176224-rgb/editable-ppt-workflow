param(
    [switch]$RemoveRuntime,
    [switch]$RemoveMarketplace,
    [string]$RuntimeRoot
)

$ErrorActionPreference = "Stop"
$RepoRoot = [System.IO.Path]::GetFullPath($PSScriptRoot)
$PluginRoot = Join-Path $RepoRoot "plugins\editable-ppt-workflow"
$PackageInfo = Get-Content -Raw -LiteralPath (Join-Path $RepoRoot "package-info.json") | ConvertFrom-Json
$MarketplaceName = [string]$PackageInfo.marketplace
$PluginName = [string]$PackageInfo.plugin

$Codex = $null
$CodexRoots = @(
    (Join-Path $env:USERPROFILE ".codex"),
    (Join-Path $env:LOCALAPPDATA "Codex"),
    (Join-Path $env:LOCALAPPDATA "OpenAI\Codex")
)
foreach ($CodexRoot in $CodexRoots) {
    $ReleaseRoot = Join-Path $CodexRoot "packages\standalone\releases"
    if (Test-Path -LiteralPath $ReleaseRoot -PathType Container) {
        $Codex = Get-ChildItem -LiteralPath $ReleaseRoot -Filter codex.exe -File -Recurse |
            Sort-Object LastWriteTime -Descending |
            Select-Object -First 1
        if ($Codex) { break }
    }
}
if (-not $Codex) { $Codex = Get-Command codex -ErrorAction SilentlyContinue }
if (-not $Codex) { throw "Codex CLI was not found." }
$CodexPath = if ($Codex.Source) { $Codex.Source } else { $Codex.FullName }

& $CodexPath plugin remove "$PluginName@$MarketplaceName"
if ($LASTEXITCODE -ne 0) { throw "Unable to remove $PluginName from $MarketplaceName." }

if ($RemoveMarketplace) {
    & $CodexPath plugin marketplace remove $MarketplaceName
    if ($LASTEXITCODE -ne 0) { throw "Unable to remove Marketplace $MarketplaceName." }
}

if ($RemoveRuntime) {
    if (-not $RuntimeRoot) {
        $RuntimeRoot = Join-Path $env:USERPROFILE ".codex\plugin-runtimes\editable-ppt-workflow"
    }
    $RuntimeRoot = [System.IO.Path]::GetFullPath($RuntimeRoot)
    . (Join-Path $PluginRoot "scripts\runtime_root_safety.ps1")
    $DefaultRuntimeRoot = Join-Path $env:USERPROFILE ".codex\plugin-runtimes\editable-ppt-workflow"
    Assert-RuntimeRootLocation -RuntimeRoot $RuntimeRoot -DefaultRuntimeRoot $DefaultRuntimeRoot -PluginRoot $PluginRoot
    if (Test-Path -LiteralPath $RuntimeRoot -PathType Container) {
        if (-not (Test-RuntimeOwnershipSentinel $RuntimeRoot)) {
            throw "Refusing to remove a runtime without the editable-ppt-workflow ownership sentinel: $RuntimeRoot"
        }
        [System.IO.Directory]::Delete($RuntimeRoot, $true)
        Write-Output "Removed isolated runtime: $RuntimeRoot"
    }
}

Write-Output "Plugin removal complete. User-created Word and PPT project folders were not searched or modified."
