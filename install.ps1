param(
    [switch]$ForceRuntime,
    [string]$RuntimeRoot,
    [string]$BinDir
)

$ErrorActionPreference = "Stop"
$RepoRoot = [System.IO.Path]::GetFullPath($PSScriptRoot)
$PluginRoot = Join-Path $RepoRoot "plugins\editable-ppt-workflow"
$PluginName = "editable-ppt-workflow"
$Manifest = Get-Content -Raw -LiteralPath (Join-Path $PluginRoot ".codex-plugin\plugin.json") | ConvertFrom-Json
$PackageInfo = Get-Content -Raw -LiteralPath (Join-Path $RepoRoot "package-info.json") | ConvertFrom-Json
$MarketplaceSource = [string]$PackageInfo.repository
$MarketplaceName = [string]$PackageInfo.marketplace

if (-not $MarketplaceSource -or -not $MarketplaceName) {
    throw "package-info.json must declare repository and marketplace."
}
if ($Manifest.version -ne $PackageInfo.pluginVersion -or $PackageInfo.workflowContractVersion -ne "word-only-v1") {
    throw "Local plugin metadata is inconsistent with the current word-only contract."
}

if (-not $IsWindows -and $PSVersionTable.PSEdition -eq "Core") {
    throw "This installer currently supports Windows only."
}

$Codex = $null
$CodexRoots = @()
if ($env:CODEX_HOME) {
    $CodexRoots += $env:CODEX_HOME
}
$CodexRoots += @(
    (Join-Path $env:USERPROFILE ".codex"),
    (Join-Path $env:LOCALAPPDATA "Codex"),
    (Join-Path $env:LOCALAPPDATA "OpenAI\Codex")
)
foreach ($CodexRoot in ($CodexRoots | Select-Object -Unique)) {
    $ReleaseRoot = Join-Path $CodexRoot "packages\standalone\releases"
    if (Test-Path -LiteralPath $ReleaseRoot -PathType Container) {
        $Codex = Get-ChildItem -LiteralPath $ReleaseRoot -Filter codex.exe -File -Recurse |
            Sort-Object LastWriteTime -Descending |
            Select-Object -First 1
        if ($Codex) {
            break
        }
    }
}
if (-not $Codex) {
    $Codex = Get-Command codex -ErrorAction SilentlyContinue
}
if (-not $Codex) {
    throw "Codex CLI is required on PATH. Install or open Codex Desktop, enable its CLI, and retry."
}
$CodexPath = if ($Codex.Source) { $Codex.Source } else { $Codex.FullName }

$Existing = (& $CodexPath plugin marketplace list 2>&1 | Out-String)
if ($Existing -match [regex]::Escape($MarketplaceName)) {
    & $CodexPath plugin marketplace upgrade $MarketplaceName
} else {
    & $CodexPath plugin marketplace add $MarketplaceSource --ref main
}
if ($LASTEXITCODE -ne 0) {
    throw "Unable to add or refresh Marketplace '$MarketplaceName' from '$MarketplaceSource'. Check network access and repository permissions."
}

& $CodexPath plugin add "$PluginName@$MarketplaceName"
if ($LASTEXITCODE -ne 0) {
    throw "Codex could not install $PluginName from $MarketplaceName."
}

$RuntimeInstaller = Join-Path $PluginRoot "scripts\install_runtime.ps1"
if (-not (Test-Path -LiteralPath $RuntimeInstaller -PathType Leaf)) {
    throw "Runtime installer is missing: $RuntimeInstaller"
}

$RuntimeArguments = @{}
if ($ForceRuntime) { $RuntimeArguments.Force = $true }
if ($RuntimeRoot) { $RuntimeArguments.RuntimeRoot = $RuntimeRoot }
if ($BinDir) { $RuntimeArguments.BinDir = $BinDir }
& $RuntimeInstaller @RuntimeArguments
if ($LASTEXITCODE -ne 0) {
    throw "Plugin runtime installation failed."
}

$VerifyArguments = @{}
if ($RuntimeRoot) { $VerifyArguments.RuntimeRoot = $RuntimeRoot }
& (Join-Path $RepoRoot "verify.ps1") @VerifyArguments
if ($LASTEXITCODE -ne 0) {
    throw "Plugin verification failed."
}

Write-Output "Installation complete. Start a new Codex task, then provide one paginated Word document to @$PluginName."
