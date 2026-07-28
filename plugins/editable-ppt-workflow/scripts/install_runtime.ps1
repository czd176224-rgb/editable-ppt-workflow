param(
    [switch]$Force,
    [string]$RuntimeRoot,
    [string]$BinDir,
    [string]$OfficeCliExe,
    [switch]$PortableSmokeTest
)

$ErrorActionPreference = "Stop"
$PluginRoot = [System.IO.Path]::GetFullPath((Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)))
$WorkflowSkill = Join-Path $PluginRoot "skills\word-to-editable-ppt"
$EditableSkill = Join-Path $PluginRoot "skills\image-to-editable-ppt"
$RunningOnWindows = [System.Environment]::OSVersion.Platform -eq [System.PlatformID]::Win32NT
$UserCodexRoot = Join-Path $env:USERPROFILE ".codex"
$DefaultRuntimeRoot = Join-Path $UserCodexRoot "plugin-runtimes\editable-ppt-workflow"
$RuntimeSafetyScript = Join-Path $PSScriptRoot "runtime_root_safety.ps1"
. $RuntimeSafetyScript

if (-not $RuntimeRoot) {
    $RuntimeRoot = $DefaultRuntimeRoot
}
if (-not $BinDir) {
    $BinDir = Join-Path $UserCodexRoot "bin"
}
$RuntimeRoot = [System.IO.Path]::GetFullPath($RuntimeRoot)
$BinDir = [System.IO.Path]::GetFullPath($BinDir)

if ($PortableSmokeTest) {
    if (-not $PSBoundParameters.ContainsKey("RuntimeRoot") -or -not $PSBoundParameters.ContainsKey("BinDir")) {
        throw "PortableSmokeTest requires explicit temporary RuntimeRoot and BinDir paths."
    }
}

foreach ($required in @(
    (Join-Path $WorkflowSkill "SKILL.md"),
    (Join-Path $WorkflowSkill "requirements.txt"),
    (Join-Path $WorkflowSkill "scripts\confirm_ui\server.py"),
    (Join-Path $EditableSkill "SKILL.md"),
    (Join-Path $PluginRoot "skills\codex-gpt-image\SKILL.md"),
    (Join-Path $PluginRoot "skills\officecli\SKILL.md")
)) {
    if (-not (Test-Path -LiteralPath $required -PathType Leaf)) {
        throw "Plugin file is missing: $required"
    }
}

$PythonCommand = Get-Command python -ErrorAction SilentlyContinue
if (-not $PythonCommand) {
    throw "Python 3.10+ is required. Install Python and enable python.exe on PATH."
}
$PythonExe = if ($PythonCommand.Source) { $PythonCommand.Source } else { $PythonCommand.Path }
$DetectedPython = & $PythonExe -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}')"
if ($LASTEXITCODE -ne 0) {
    throw "Unable to execute Python."
}
$DetectedParts = $DetectedPython.Trim().Split('.')
if ([int]$DetectedParts[0] -lt 3 -or ([int]$DetectedParts[0] -eq 3 -and [int]$DetectedParts[1] -lt 10)) {
    throw "Python 3.10 or newer is required; detected $DetectedPython."
}

$RuntimeRoot = Initialize-EditablePptRuntimeRoot `
    -RuntimeRoot $RuntimeRoot `
    -DefaultRuntimeRoot $DefaultRuntimeRoot `
    -PluginRoot $PluginRoot `
    -Force:$Force
New-Item -ItemType Directory -Force -Path $BinDir | Out-Null

$PluginManifest = Get-Content -Raw -LiteralPath (Join-Path $PluginRoot ".codex-plugin\plugin.json") | ConvertFrom-Json
$PluginVersion = [string]$PluginManifest.version
$InstallStatePath = Join-Path $RuntimeRoot "runtime_install_state.json"
$InstallState = [ordered]@{
    schemaVersion = 1
    plugin_version = $PluginVersion
    workflow_dependencies_ready = $false
    editable_cli_ready = $false
}
if (Test-Path -LiteralPath $InstallStatePath -PathType Leaf) {
    try {
        $PreviousState = Get-Content -Raw -LiteralPath $InstallStatePath | ConvertFrom-Json
        if ($PreviousState.schemaVersion -eq 1 -and $PreviousState.plugin_version -eq $PluginVersion) {
            $InstallState.workflow_dependencies_ready = $PreviousState.workflow_dependencies_ready -eq $true
            $InstallState.editable_cli_ready = $PreviousState.editable_cli_ready -eq $true
        }
    } catch {
        Write-Warning "Ignoring unreadable runtime install state: $InstallStatePath"
    }
}

function Save-RuntimeInstallState {
    $InstallState | ConvertTo-Json | Set-Content -LiteralPath $InstallStatePath -Encoding utf8
}

$WorkflowVenv = Join-Path $RuntimeRoot "workflow"
if (-not (Test-Path -LiteralPath (Join-Path $WorkflowVenv "Scripts\python.exe") -PathType Leaf)) {
    & $PythonExe -m venv $WorkflowVenv
    if ($LASTEXITCODE -ne 0) { throw "Unable to create the workflow virtual environment." }
}
$WorkflowPython = Join-Path $WorkflowVenv "Scripts\python.exe"
$WorkflowStageReady = $InstallState.workflow_dependencies_ready -eq $true
if ($WorkflowStageReady) {
    & $WorkflowPython -c "import flask, jsonschema, PIL, fitz, docx, pptx"
    $WorkflowStageReady = $LASTEXITCODE -eq 0
}
if (-not $WorkflowStageReady) {
    $InstallState.workflow_dependencies_ready = $false
    Save-RuntimeInstallState
    & $WorkflowPython -m pip install --disable-pip-version-check --upgrade pip
    if ($LASTEXITCODE -ne 0) { throw "Unable to update pip in the workflow runtime." }
    & $WorkflowPython -m pip install --disable-pip-version-check -r (Join-Path $WorkflowSkill "requirements.txt")
    if ($LASTEXITCODE -ne 0) { throw "Unable to install workflow prerequisites." }
    & $WorkflowPython -m pip check
    if ($LASTEXITCODE -ne 0) { throw "Workflow prerequisite check failed." }
    $InstallState.workflow_dependencies_ready = $true
    Save-RuntimeInstallState
} else {
    Write-Output "Reusing workflow dependencies for plugin $PluginVersion."
}

$EditableVenv = Join-Path $RuntimeRoot "editable-ppt"
if (-not (Test-Path -LiteralPath (Join-Path $EditableVenv "Scripts\python.exe") -PathType Leaf)) {
    & $PythonExe -m venv $EditableVenv
    if ($LASTEXITCODE -ne 0) { throw "Unable to create the editable-PPT virtual environment." }
}
$EditablePython = Join-Path $EditableVenv "Scripts\python.exe"
$EditExe = Join-Path $EditableVenv "Scripts\editppt.exe"
$EditableStageReady = $InstallState.editable_cli_ready -eq $true -and (Test-Path -LiteralPath $EditExe -PathType Leaf)
if ($EditableStageReady) {
    & $EditablePython -c "import editppt"
    $EditableStageReady = $LASTEXITCODE -eq 0
}
if (-not $EditableStageReady) {
    $InstallState.editable_cli_ready = $false
    Save-RuntimeInstallState
    & $EditablePython -m pip install --disable-pip-version-check --upgrade pip
    if ($LASTEXITCODE -ne 0) { throw "Unable to update pip in the editable-PPT runtime." }
    & $EditablePython -m pip install --disable-pip-version-check --upgrade (Join-Path $EditableSkill "cli")
    if ($LASTEXITCODE -ne 0) { throw "Unable to install the editable-PPT CLI." }
    & $EditablePython -m pip check
    if ($LASTEXITCODE -ne 0) { throw "Editable-PPT prerequisite check failed." }
    $InstallState.editable_cli_ready = $true
    Save-RuntimeInstallState
} else {
    Write-Output "Reusing editable-PPT CLI for plugin $PluginVersion."
}

# Install a self-contained copy of the current Word workflow beside the two
# virtual environments.  The editppt wheel imports this explicit runtime
# boundary through a .pth file; it never guesses a repository sibling path.
$CurrentWorkflowRoot = Join-Path $RuntimeRoot "current-workflow"
if (Test-Path -LiteralPath $CurrentWorkflowRoot) {
    Remove-Item -LiteralPath $CurrentWorkflowRoot -Recurse -Force
}
New-Item -ItemType Directory -Path $CurrentWorkflowRoot | Out-Null
foreach ($name in @("scripts", "schemas", "template")) {
    Copy-Item -LiteralPath (Join-Path $WorkflowSkill $name) -Destination $CurrentWorkflowRoot -Recurse
}
$CurrentWorkflowScripts = Join-Path $CurrentWorkflowRoot "scripts"
$EditableSitePackages = (& $EditablePython -c "import site; print(site.getsitepackages()[0])").Trim()
if ($LASTEXITCODE -ne 0 -or -not $EditableSitePackages) {
    throw "Unable to locate editable-PPT site-packages for workflow injection."
}
Set-Content -LiteralPath (Join-Path $EditableSitePackages "word_to_editable_ppt_current_workflow.pth") -Value $CurrentWorkflowScripts -Encoding ascii
& $EditablePython -c "import workflow_state, final_mechanical_qa; print('editppt-workflow-boundary=ok')"
if ($LASTEXITCODE -ne 0) { throw "Editable-PPT current workflow injection failed." }
if ($RunningOnWindows -and -not $PortableSmokeTest) {
    & $EditablePython -c "import win32com.client; print('editppt-win32com=ok')"
    if ($LASTEXITCODE -ne 0) { throw "Editable-PPT Windows COM dependency probe failed." }
}

$EditWrapper = @"
@echo off
"$EditExe" %*
"@
Set-Content -LiteralPath (Join-Path $BinDir "editppt.CMD") -Value $EditWrapper -Encoding ascii

$ResolvedOfficeExe = $null
if (-not $PortableSmokeTest) {
    if ($OfficeCliExe) {
        $ResolvedOfficeExe = [System.IO.Path]::GetFullPath($OfficeCliExe)
    } else {
        $ResolvedOfficeExe = Join-Path $env:LOCALAPPDATA "OfficeCLI\officecli.exe"
    }
    if (-not (Test-Path -LiteralPath $ResolvedOfficeExe -PathType Leaf)) {
        Write-Output "Installing OfficeCLI from its official installer..."
        $OfficeInstaller = Invoke-RestMethod -Uri "https://d.officecli.ai/install.ps1"
        & ([scriptblock]::Create($OfficeInstaller))
    }
    if (-not (Test-Path -LiteralPath $ResolvedOfficeExe -PathType Leaf)) {
        throw "OfficeCLI installation did not create $ResolvedOfficeExe"
    }
    $OfficeWrapper = @"
@echo off
"$ResolvedOfficeExe" %*
"@
    Set-Content -LiteralPath (Join-Path $BinDir "officecli.CMD") -Value $OfficeWrapper -Encoding ascii

    $UserPath = [Environment]::GetEnvironmentVariable("Path", "User")
    $PathParts = @($UserPath -split ";" | Where-Object { $_ -and $_ -ne $BinDir })
    [Environment]::SetEnvironmentVariable("Path", (($BinDir + $PathParts) -join ";"), "User")
}

$env:Path = "$BinDir;$env:Path"
$env:CODEX_GPT_IMAGE_SKILL = Join-Path $PluginRoot "skills\codex-gpt-image"
$env:EDITPPT_EXE = $EditExe
if ($ResolvedOfficeExe) {
    $env:OFFICECLI_EXE = $ResolvedOfficeExe
}

$ReportPath = Join-Path $RuntimeRoot "runtime_report.json"
if ($PortableSmokeTest) {
    $WorkflowScripts = $CurrentWorkflowScripts
    $PreviousPythonPath = $env:PYTHONPATH
    $env:PYTHONPATH = if ($PreviousPythonPath) { "$WorkflowScripts;$PreviousPythonPath" } else { $WorkflowScripts }
    try {
        & $WorkflowPython -c "import flask, jsonschema, PIL, fitz, docx, pptx; import confirm_ui.server, workflow_state; print('workflow-import-smoke=ok')"
        if ($LASTEXITCODE -ne 0) { throw "Workflow import smoke test failed." }
        & $WorkflowPython (Join-Path $PSScriptRoot "portable_e2e_smoke.py") --editppt $EditExe --output (Join-Path $RuntimeRoot "portable-e2e")
        if ($LASTEXITCODE -ne 0) { throw "Editable-PPT record/finalize portable E2E failed." }
    } finally {
        $env:PYTHONPATH = $PreviousPythonPath
    }
    [ordered]@{
        portable_smoke_test = $true
        workflow_imports = "ok"
        editppt_cli = "record-finalize-ok"
        win32com_import = "skipped-portable"
        workflow_python = $WorkflowPython
        editppt = $EditExe
    } | ConvertTo-Json | Set-Content -LiteralPath $ReportPath -Encoding utf8
    Write-Output "Portable clean-install smoke passed."
    Write-Output "Runtime report: $ReportPath"
    return
}

& $WorkflowPython (Join-Path $WorkflowSkill "scripts\doctor.py") --check-powerpoint --smoke-test --json $ReportPath
if ($LASTEXITCODE -ne 0) {
    throw "Plugin runtime diagnostics failed. Review $ReportPath."
}
$Report = Get-Content -Raw -LiteralPath $ReportPath | ConvertFrom-Json
if (-not $Report.workflow_ready) {
    throw "Plugin runtime is incomplete. Review $ReportPath and confirm Codex sign-in, editppt, and OfficeCLI."
}
if (-not $Report.render_backend_available) {
    throw "Install Microsoft PowerPoint or LibreOffice, then retry."
}
if (-not $Report.high_quality_ready) {
    Write-Warning "The runtime works, but a CJK font or render backend is not ready. Review $ReportPath."
}

Write-Output "Editable PPT word-only runtime is ready."
Write-Output "Runtime report: $ReportPath"
