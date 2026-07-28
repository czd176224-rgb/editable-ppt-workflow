"""Execute the PowerShell runtime ownership/deletion safety smoke test."""

from __future__ import annotations

import platform
import re
import shutil
import subprocess
import tomllib
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[5]
SMOKE = REPO_ROOT / "plugins/editable-ppt-workflow/scripts/test_install_runtime_safety.ps1"
INSTALLER = REPO_ROOT / "plugins/editable-ppt-workflow/scripts/install_runtime.ps1"
PORTABLE_E2E = REPO_ROOT / "plugins/editable-ppt-workflow/scripts/portable_e2e_smoke.py"
VERIFY = REPO_ROOT / "verify.ps1"
EDITABLE_PYPROJECT = (
    REPO_ROOT
    / "plugins/editable-ppt-workflow/skills/image-to-editable-ppt/cli/pyproject.toml"
)


@pytest.mark.skipif(platform.system() != "Windows", reason="Windows PowerShell installer contract")
def test_runtime_install_safety_smoke():
    powershell = shutil.which("powershell.exe") or shutil.which("pwsh.exe")
    assert powershell, "PowerShell is required on Windows"
    completed = subprocess.run(
        [powershell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(SMOKE)],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "runtime-root-safety-smoke=ok" in completed.stdout


def test_portable_installer_injects_current_workflow_and_runs_real_finalize_smoke():
    """A --help-only smoke cannot prove that the separately installed CLI can finalize."""
    source = INSTALLER.read_text(encoding="utf-8")

    assert "current-workflow" in source
    assert ".pth" in source
    assert "portable_e2e_smoke.py" in source
    assert "& $EditExe --help" not in source
    e2e = PORTABLE_E2E.read_text(encoding="utf-8")
    assert re.search(r'"run",\s*"record"', e2e)
    assert re.search(r'"run",\s*"finalize"', e2e)


def test_verify_imports_only_the_installed_current_workflow_copy():
    """Verification must not hide a broken package by importing repository scripts."""
    source = VERIFY.read_text(encoding="utf-8")

    assert '$CurrentWorkflowRoot = Join-Path $RuntimeRoot "current-workflow"' in source
    assert '$WorkflowScripts = Join-Path $CurrentWorkflowRoot "scripts"' in source
    assert '$WorkflowScripts = Join-Path $WorkflowSkill "scripts"' not in source
    assert '& $EditablePython -c "import editppt, workflow_state, final_mechanical_qa;' in source
    assert 'import confirm_ui.server, workflow_state, final_mechanical_qa' not in source


def test_editable_runtime_declares_windows_only_powerpoint_com_dependency():
    metadata = tomllib.loads(EDITABLE_PYPROJECT.read_text(encoding="utf-8"))

    assert "pywin32>=306; sys_platform == 'win32'" in metadata["project"]["dependencies"]


def test_windows_nonportable_runtime_probes_real_win32com_and_portable_records_skip():
    installer = INSTALLER.read_text(encoding="utf-8")
    verifier = VERIFY.read_text(encoding="utf-8")
    probe = "import win32com.client; print('editppt-win32com=ok')"

    assert probe in installer
    assert probe in verifier
    assert "$RunningOnWindows -and -not $PortableSmokeTest" in installer
    assert "$RunningOnWindows -and -not $PortableSmokeTest" in verifier
    assert 'win32com_import = "skipped-portable"' in installer
    assert '$Report.win32com_import -ne "skipped-portable"' in verifier


def test_optional_local_renderer_never_blocks_runtime_installation():
    installer = INSTALLER.read_text(encoding="utf-8")

    assert 'throw "Install Microsoft PowerPoint or LibreOffice, then retry."' not in installer
    assert "Write-Warning" in installer


def test_installed_current_workflow_contains_the_shared_office_resolver():
    installer = INSTALLER.read_text(encoding="utf-8")

    assert 'Join-Path $PluginRoot "scripts\\runtime_office.py"' in installer
    assert 'Join-Path $CurrentWorkflowScripts "runtime_office.py"' in installer
