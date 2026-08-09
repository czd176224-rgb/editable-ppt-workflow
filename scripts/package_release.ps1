param(
    [string]$SourceRoot,
    [string]$OutputDirectory
)

$ErrorActionPreference = "Stop"
if (-not $SourceRoot) { $SourceRoot = Split-Path -Parent $PSScriptRoot }
if (-not $OutputDirectory) { $OutputDirectory = Join-Path $SourceRoot "dist" }
$SourceRoot = [System.IO.Path]::GetFullPath($SourceRoot)
$OutputDirectory = [System.IO.Path]::GetFullPath($OutputDirectory)

if (-not (Test-Path -LiteralPath (Join-Path $SourceRoot ".git") -PathType Container)) {
    & python (Join-Path $SourceRoot "scripts\check_public_release.py") $SourceRoot
    if ($LASTEXITCODE -ne 0) { throw "Refusing to package an invalid exported public snapshot." }
}

$Package = Get-Content -Raw -Encoding UTF8 -LiteralPath (Join-Path $SourceRoot "package-info.json") | ConvertFrom-Json
$Version = [string]$Package.pluginVersion
$SafeVersion = $Version -replace '[^A-Za-z0-9._+-]', '-'
New-Item -ItemType Directory -Force -Path $OutputDirectory | Out-Null
$ZipPath = Join-Path $OutputDirectory "editable-ppt-workflow-$SafeVersion-windows.zip"
$ChecksumPath = Join-Path $OutputDirectory "SHA256SUMS.txt"
if (Test-Path -LiteralPath $ZipPath) { Remove-Item -LiteralPath $ZipPath -Force }

& python (Join-Path $SourceRoot "scripts\build_release_archive.py") --source $SourceRoot --output $ZipPath
if ($LASTEXITCODE -ne 0) { throw "Deterministic tracked-source archive build failed." }

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
