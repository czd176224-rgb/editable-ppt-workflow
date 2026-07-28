param(
    [string]$SourceRoot,
    [string]$OutputDirectory
)

$ErrorActionPreference = "Stop"
if (-not $SourceRoot) { $SourceRoot = Split-Path -Parent $PSScriptRoot }
if (-not $OutputDirectory) { $OutputDirectory = Join-Path $SourceRoot "dist" }
$SourceRoot = [System.IO.Path]::GetFullPath($SourceRoot)
$OutputDirectory = [System.IO.Path]::GetFullPath($OutputDirectory)

& python (Join-Path $SourceRoot "scripts\check_public_release.py") $SourceRoot
if ($LASTEXITCODE -ne 0) { throw "Refusing to package an invalid public snapshot." }

$Package = Get-Content -Raw -LiteralPath (Join-Path $SourceRoot "package-info.json") | ConvertFrom-Json
$Version = [string]$Package.pluginVersion
$SafeVersion = $Version -replace '[^A-Za-z0-9._+-]', '-'
New-Item -ItemType Directory -Force -Path $OutputDirectory | Out-Null
$ZipPath = Join-Path $OutputDirectory "editable-ppt-workflow-$SafeVersion-windows.zip"
$ChecksumPath = Join-Path $OutputDirectory "SHA256SUMS.txt"
if (Test-Path -LiteralPath $ZipPath) { Remove-Item -LiteralPath $ZipPath -Force }

$StageRoot = Join-Path ([System.IO.Path]::GetTempPath()) ("editable-ppt-release-" + [guid]::NewGuid().ToString("N"))
try {
    New-Item -ItemType Directory -Path $StageRoot | Out-Null
    $Allowlist = Get-Content -Raw -LiteralPath (Join-Path $SourceRoot "public-release-files.json") | ConvertFrom-Json
    foreach ($relative in $Allowlist.files) {
        $source = Join-Path $SourceRoot ([string]$relative)
        $destination = Join-Path $StageRoot ([string]$relative)
        $parent = Split-Path -Parent $destination
        if ($parent) { New-Item -ItemType Directory -Force -Path $parent | Out-Null }
        Copy-Item -LiteralPath $source -Destination $destination -Recurse -Force
    }
    Copy-Item -LiteralPath (Join-Path $SourceRoot "public-release-audit.json") -Destination (Join-Path $StageRoot "public-release-audit.json") -Force -ErrorAction SilentlyContinue
    $ArchiveItems = @(Get-ChildItem -LiteralPath $StageRoot -Force | Select-Object -ExpandProperty FullName)
    Compress-Archive -Path $ArchiveItems -DestinationPath $ZipPath -CompressionLevel Optimal
} finally {
    if (Test-Path -LiteralPath $StageRoot) {
        [System.IO.Directory]::Delete($StageRoot, $true)
    }
}

$Sha256 = [System.Security.Cryptography.SHA256]::Create()
$Stream = [System.IO.File]::OpenRead($ZipPath)
try {
    $Hash = ([System.BitConverter]::ToString($Sha256.ComputeHash($Stream))).Replace("-", "").ToLowerInvariant()
} finally {
    $Stream.Dispose()
    $Sha256.Dispose()
}
"$Hash  $([System.IO.Path]::GetFileName($ZipPath))" | Set-Content -LiteralPath $ChecksumPath -Encoding ascii
Write-Output $ZipPath
Write-Output $ChecksumPath
