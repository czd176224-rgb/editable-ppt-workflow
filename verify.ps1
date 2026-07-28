param(
    [string]$RuntimeRoot,
    [switch]$PortableSmokeTest
)

$ErrorActionPreference = "Stop"
$RepoRoot = [System.IO.Path]::GetFullPath($PSScriptRoot)
$PluginRoot = Join-Path $RepoRoot "plugins\editable-ppt-workflow"
$WorkflowSkill = Join-Path $PluginRoot "skills\word-to-editable-ppt"
$ManifestPath = Join-Path $PluginRoot ".codex-plugin\plugin.json"
$PackageInfoPath = Join-Path $RepoRoot "package-info.json"
$PolicyScanner = Join-Path $PluginRoot "scripts\check_current_runtime.py"
$ExpectedWorkflowContract = "word-only-v1"
$RunningOnWindows = [System.Environment]::OSVersion.Platform -eq [System.PlatformID]::Win32NT

if (-not $RuntimeRoot) {
    $RuntimeRoot = Join-Path $env:USERPROFILE ".codex\plugin-runtimes\editable-ppt-workflow"
}
$RuntimeRoot = [System.IO.Path]::GetFullPath($RuntimeRoot)
$WorkflowPython = Join-Path $RuntimeRoot "workflow\Scripts\python.exe"
$EditablePython = Join-Path $RuntimeRoot "editable-ppt\Scripts\python.exe"
$EditPptExe = Join-Path $RuntimeRoot "editable-ppt\Scripts\editppt.exe"
$CurrentWorkflowRoot = Join-Path $RuntimeRoot "current-workflow"
$WorkflowScripts = Join-Path $CurrentWorkflowRoot "scripts"

foreach ($required in @($ManifestPath, $PackageInfoPath, $PolicyScanner, $WorkflowPython, $EditablePython, $EditPptExe)) {
    if (-not (Test-Path -LiteralPath $required -PathType Leaf)) {
        throw "Verification prerequisite is missing: $required"
    }
}
if (-not (Test-Path -LiteralPath $WorkflowScripts -PathType Container)) {
    throw "Installed current workflow scripts are missing: $WorkflowScripts"
}

$Manifest = Get-Content -Raw -LiteralPath $ManifestPath | ConvertFrom-Json
$PackageInfo = Get-Content -Raw -LiteralPath $PackageInfoPath | ConvertFrom-Json
if ($Manifest.name -ne "editable-ppt-workflow") {
    throw "Unexpected plugin name: $($Manifest.name)"
}
if ($PackageInfo.pluginVersion -ne $Manifest.version) {
    throw "package-info pluginVersion does not match plugin manifest version"
}
if ($PackageInfo.workflowContractVersion -ne $ExpectedWorkflowContract) {
    throw "package-info workflowContractVersion does not match the current workflow contract"
}
if ($PackageInfo.requiredUserFiles -ne 1) {
    throw "package-info must declare exactly one required user file"
}
if ($PackageInfo.requiredHumanConfirmationPhaseCount -ne 1) {
    throw "package-info must declare exactly one human confirmation phase"
}
if ($PackageInfo.confirmationInteraction -ne "embedded-three-stage-browser") {
    throw "package-info must declare the embedded three-stage browser interaction"
}
if ($PackageInfo.initialImageEndpoint -ne "images/generations") {
    throw "Initial page generation must use images/generations"
}
if ($PackageInfo.localRepairEndpoint -ne "images/edits") {
    throw "Local page repair must use images/edits"
}

& $WorkflowPython $PolicyScanner --repo-root $RepoRoot
if ($LASTEXITCODE -ne 0) {
    throw "Current-only runtime policy failed."
}

$PreviousPythonPath = $env:PYTHONPATH
$env:PYTHONPATH = if ($PreviousPythonPath) { "$WorkflowScripts;$PreviousPythonPath" } else { $WorkflowScripts }
try {
    & $WorkflowPython -c "import flask, jsonschema, PIL, fitz, docx, pptx; import confirm_ui.server, workflow_state; print('workflow-runtime-imports=ok')"
    if ($LASTEXITCODE -ne 0) {
        throw "Workflow runtime import verification failed."
    }
} finally {
    $env:PYTHONPATH = $PreviousPythonPath
}

& $EditablePython -c "import editppt, workflow_state, final_mechanical_qa; print('editable-runtime-imports=ok')"
if ($LASTEXITCODE -ne 0) {
    throw "Editable-PPT runtime import verification failed."
}
if ($RunningOnWindows -and -not $PortableSmokeTest) {
    & $EditablePython -c "import win32com.client; print('editppt-win32com=ok')"
    if ($LASTEXITCODE -ne 0) {
        throw "Editable-PPT Windows COM dependency verification failed."
    }
}

& $EditPptExe --help | Out-Null
if ($LASTEXITCODE -ne 0) {
    throw "Editable-PPT CLI verification failed."
}

if ($PortableSmokeTest) {
    $ReportPath = Join-Path $RuntimeRoot "runtime_report.json"
    if (-not (Test-Path -LiteralPath $ReportPath -PathType Leaf)) {
        throw "Portable runtime report is missing: $ReportPath"
    }
    $Report = Get-Content -Raw -LiteralPath $ReportPath | ConvertFrom-Json
    if ($Report.portable_smoke_test -ne $true -or $Report.workflow_imports -ne "ok" -or $Report.editppt_cli -ne "record-finalize-ok" -or $Report.win32com_import -ne "skipped-portable") {
        throw "Portable runtime report did not record a successful clean-install smoke."
    }
} else {
    & $WorkflowPython (Join-Path $WorkflowSkill "scripts\doctor.py") --check-powerpoint --smoke-test --require-high-quality
    if ($LASTEXITCODE -ne 0) {
        throw "High-quality workflow verification failed."
    }
}

Write-Output "Verified $($Manifest.name) $($Manifest.version): one Word input, one embedded three-stage confirmation phase, current-only runtime."
