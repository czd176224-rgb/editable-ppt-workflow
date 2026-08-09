param(
    [string]$RuntimeRoot,
    [switch]$PortableSmokeTest,
    [switch]$MetadataOnly
)

$ErrorActionPreference = "Stop"
$RepoRoot = [System.IO.Path]::GetFullPath($PSScriptRoot)
$PluginRoot = Join-Path $RepoRoot "plugins\editable-ppt-workflow"
$WorkflowSkill = Join-Path $PluginRoot "skills\run-word-to-ppt-workflow"
$ManifestPath = Join-Path $PluginRoot ".codex-plugin\plugin.json"
$PackageInfoPath = Join-Path $RepoRoot "package-info.json"
$PolicyScanner = Join-Path $PluginRoot "scripts\check_current_runtime.py"
$ExpectedWorkflowContract = "word-ppt-workflow-v5"
$RunningOnWindows = [System.Environment]::OSVersion.Platform -eq [System.PlatformID]::Win32NT

if (-not $RuntimeRoot) {
    $RuntimeRoot = Join-Path $env:USERPROFILE ".codex\plugin-runtimes\editable-ppt-workflow-fixed-canvas-cm-v2"
}
$RuntimeRoot = [System.IO.Path]::GetFullPath($RuntimeRoot)
$WorkflowPython = Join-Path $RuntimeRoot "workflow\Scripts\python.exe"
$EditablePython = Join-Path $RuntimeRoot "editable-ppt\Scripts\python.exe"
$EditPptExe = Join-Path $RuntimeRoot "editable-ppt\Scripts\editppt.exe"
$PreflightManifest = Get-Content -Raw -Encoding UTF8 -LiteralPath $ManifestPath | ConvertFrom-Json
$WorkflowPackageName = "workflow-" + ([string]$PreflightManifest.version -replace '[^A-Za-z0-9._-]', '_')
$CurrentWorkflowRoot = Join-Path $RuntimeRoot $WorkflowPackageName
$WorkflowScripts = Join-Path $CurrentWorkflowRoot "scripts"

foreach ($required in @($ManifestPath, $PackageInfoPath, $PolicyScanner)) {
    if (-not (Test-Path -LiteralPath $required -PathType Leaf)) {
        throw "Verification prerequisite is missing: $required"
    }
}

$Manifest = Get-Content -Raw -Encoding UTF8 -LiteralPath $ManifestPath | ConvertFrom-Json
$PackageInfo = Get-Content -Raw -Encoding UTF8 -LiteralPath $PackageInfoPath | ConvertFrom-Json
$Marketplace = Get-Content -Raw -Encoding UTF8 -LiteralPath (Join-Path $RepoRoot ".agents\plugins\marketplace.json") | ConvertFrom-Json
if ($Manifest.name -ne "editable-ppt-workflow") {
    throw "Unexpected plugin name: $($Manifest.name)"
}
if ($PackageInfo.pluginVersion -ne $Manifest.version) {
    throw "package-info pluginVersion does not match plugin manifest version"
}
if ($PackageInfo.workflowContractVersion -ne $ExpectedWorkflowContract) {
    throw "package-info workflowContractVersion does not match the current workflow contract"
}
if ($PackageInfo.marketplacePreviewIdentity -ne $Marketplace.name) {
    throw "package-info marketplacePreviewIdentity does not match the local Marketplace name"
}
if ($Marketplace.interface.displayName -notmatch [regex]::Escape([string]$Manifest.version)) {
    throw "local Marketplace displayName does not contain the plugin version"
}
if ($PackageInfo.requiredUserFiles -ne 2) {
    throw "package-info must declare the paginated Word and required SVG logo"
}
if ($PackageInfo.requiredHumanConfirmationPhaseCount -ne 1) {
    throw "package-info must declare exactly one human confirmation phase"
}
if ($PackageInfo.confirmationInteraction -ne "single-global-confirmation") {
    throw "package-info must declare the single global confirmation interaction"
}
if ($PackageInfo.uiPreviewImagePolicy -ne "project-audit-only-never-image-input") {
    throw "UI preview audit image must be excluded from image generation"
}
if ($PackageInfo.imageSourcePixels -ne "exact-1904x896-17:8-with-actual-dimension-receipt") {
    throw "package-info must declare the exact 1904x896 17:8 body-image source profile"
}
if ($PackageInfo.bodyImageSizes.speed -ne "1904x896" -or
    $PackageInfo.bodyImageSizes.balanced -ne "1904x896" -or
    $PackageInfo.bodyImageSizes.quality -ne "1904x896") {
    throw "package-info must keep all profiles on the exact 1904x896 V5 canvas"
}
if ($PackageInfo.bodyImageAspectPolicy -ne "17:8-relative-error-at-most-0.01" -or $PackageInfo.everyPageCallsImage2 -ne $true) {
    throw "package-info must declare every-page Image2 and the 17:8 one-percent aspect gate"
}
if ($PackageInfo.geometryTolerancePercent -ne 0.1) {
    throw "package-info must declare the 0.1 percent geometry tolerance"
}
if ($PackageInfo.initialImageEndpoint -ne "images/generations-or-edits-by-reference-presence") {
    throw "Initial page generation must declare its reference-aware Images endpoint"
}
if ($PackageInfo.localRepairEndpoint -ne "images/edits") {
    throw "Local page repair must use images/edits"
}

if ($MetadataOnly) {
    Write-Output "verify-metadata-preflight=ok"
    exit 0
}

foreach ($required in @($WorkflowPython, $EditablePython, $EditPptExe)) {
    if (-not (Test-Path -LiteralPath $required -PathType Leaf)) {
        throw "Verification prerequisite is missing: $required"
    }
}
if (-not (Test-Path -LiteralPath $WorkflowScripts -PathType Container)) {
    throw "Installed current workflow scripts are missing: $WorkflowScripts"
}

& $WorkflowPython $PolicyScanner --repo-root $RepoRoot
if ($LASTEXITCODE -ne 0) {
    throw "Current-only runtime policy failed."
}

$PreviousPythonPath = $env:PYTHONPATH
$env:PYTHONPATH = if ($PreviousPythonPath) { "$WorkflowScripts;$PreviousPythonPath" } else { $WorkflowScripts }
try {
    & $WorkflowPython -c "import flask, jsonschema, PIL, pypdf, pypdfium2, docx, pptx; import confirm_ui.server, workflow_state, workflow_v5_dag, workflow_v5_delivery, workflow_v5_final_qa_gateway, workflow_v5_material_search; print('workflow-runtime-imports=ok')"
    if ($LASTEXITCODE -ne 0) {
        throw "Workflow runtime import verification failed."
    }
} finally {
    $env:PYTHONPATH = $PreviousPythonPath
}

if ($PortableSmokeTest) {
    # install_runtime already executed the isolated editppt.exe for a real V4
    # object build/validate. Recheck the installed package from the workflow
    # venv here; the report below attests the separate editable CLI execution.
    & $WorkflowPython -c "import editppt; print('portable-editppt-package-import=ok')"
    if ($LASTEXITCODE -ne 0) { throw "Portable editppt package import verification failed." }
} else {
    & $EditablePython -c "import editppt, workflow_state, final_mechanical_qa; print('editable-runtime-imports=ok')"
    if ($LASTEXITCODE -ne 0) { throw "Editable-PPT runtime import verification failed." }
}
if ($RunningOnWindows -and -not $PortableSmokeTest) {
    & $EditablePython -c "import win32com.client; print('editppt-win32com=ok')"
    if ($LASTEXITCODE -ne 0) {
        throw "Editable-PPT Windows COM dependency verification failed."
    }
}

if (-not $PortableSmokeTest) {
    & $EditPptExe --help | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "Editable-PPT CLI verification failed." }
}

if ($PortableSmokeTest) {
    $ReportPath = Join-Path $RuntimeRoot "runtime_report.json"
    if (-not (Test-Path -LiteralPath $ReportPath -PathType Leaf)) {
        throw "Portable runtime report is missing: $ReportPath"
    }
    $Report = Get-Content -Raw -Encoding UTF8 -LiteralPath $ReportPath | ConvertFrom-Json
    if ($Report.portable_smoke_test -ne $true -or $Report.workflow_imports -ne "ok" -or $Report.editppt_cli -ne "v4-build-validate-ok" -or $Report.win32com_import -ne "skipped-portable") {
        throw "Portable runtime report did not record a successful clean-install smoke."
    }
} else {
    $PreviousImageSkill = $env:CODEX_GPT_IMAGE_SKILL
    $env:CODEX_GPT_IMAGE_SKILL = Join-Path $RuntimeRoot "generate-slide-body-image"
    try {
        & $WorkflowPython (Join-Path $WorkflowScripts "doctor.py") --check-powerpoint --smoke-test --require-high-quality
        if ($LASTEXITCODE -ne 0) {
            throw "High-quality workflow verification failed."
        }
    } finally {
        if ($null -eq $PreviousImageSkill) { Remove-Item Env:CODEX_GPT_IMAGE_SKILL -ErrorAction SilentlyContinue }
        else { $env:CODEX_GPT_IMAGE_SKILL = $PreviousImageSkill }
    }
}

Write-Output "Verified $($Manifest.name) $($Manifest.version): word-ppt-workflow-v5, every-page Image2 visual authority, high-fidelity editable reconstruction, paired final QA, authentic-pixel custody, mandatory Office validation, fixed-canvas-cm-v2."
