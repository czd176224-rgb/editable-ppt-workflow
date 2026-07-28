param(
    [switch]$ForceRuntime
)

$ErrorActionPreference = "Stop"
& (Join-Path $PSScriptRoot "install.ps1") -ForceRuntime:$ForceRuntime
if ($LASTEXITCODE -ne 0) {
    throw "Editable PPT Workflow update failed."
}
Write-Output "Update complete. Restart Codex and start a new task to load the updated plugin."
