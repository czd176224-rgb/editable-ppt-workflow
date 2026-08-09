"""Execute the PowerShell runtime ownership/deletion safety smoke test."""

from __future__ import annotations

import platform
import re
import os
import shutil
import subprocess
import sys
import tomllib
import venv
from pathlib import Path

import pytest
from PIL import Image


REPO_ROOT = Path(__file__).resolve().parents[5]
SMOKE = REPO_ROOT / "plugins/editable-ppt-workflow/scripts/test_install_runtime_safety.ps1"
INSTALLER = REPO_ROOT / "plugins/editable-ppt-workflow/scripts/install_runtime.ps1"
PORTABLE_E2E = REPO_ROOT / "plugins/editable-ppt-workflow/scripts/portable_e2e_smoke.py"
VERIFY = REPO_ROOT / "verify.ps1"
EDITABLE_PYPROJECT = (
    REPO_ROOT
    / "plugins/editable-ppt-workflow/skills/reconstruct-editable-slide/cli/pyproject.toml"
)
EDITABLE_CLI_ROOT = (
    REPO_ROOT
    / "plugins/editable-ppt-workflow/skills/reconstruct-editable-slide/cli"
)
BACKGROUND_TEXT_DETECTOR = (
    REPO_ROOT
    / "plugins/editable-ppt-workflow/skills/run-word-to-ppt-workflow/scripts/background_text_detector.py"
)
WORD_WORKFLOW_SCRIPTS = BACKGROUND_TEXT_DETECTOR.parent


@pytest.mark.skipif(platform.system() != "Windows", reason="Windows PowerShell verifier contract")
def test_verify_metadata_preflight_accepts_current_single_global_confirmation_contract():
    """A real install must not reject the package's current one-confirmation metadata."""
    powershell = shutil.which("powershell.exe") or shutil.which("pwsh.exe")
    assert powershell, "PowerShell is required on Windows"
    completed = subprocess.run(
        [
            powershell,
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(VERIFY),
            "-MetadataOnly",
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "verify-metadata-preflight=ok" in completed.stdout


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


def test_portable_installer_injects_current_workflow_and_runs_real_v4_object_build_smoke():
    """A --help-only smoke cannot prove that the separately installed CLI builds V4 objects."""
    source = INSTALLER.read_text(encoding="utf-8")

    assert '$WorkflowPackageName = "workflow-"' in source
    assert "$WorkflowStage" in source
    assert ".pth" in source
    assert "portable_e2e_smoke.py" in source
    assert "& $EditExe --help" not in source
    e2e = PORTABLE_E2E.read_text(encoding="utf-8")
    assert re.search(r'"page",\s*"build"', e2e)
    assert re.search(r'"page",\s*"validate"', e2e)
    assert "from prepare_run import prepare" in e2e
    assert '"word-ppt-workflow-v4"' in e2e
    assert '"reconstruction_contract_version": "editable-image-v3"' in e2e


def test_workflow_runtime_installs_its_declared_editppt_package_dependency():
    """Word orchestration imports editppt state/finalize modules and must own that package."""
    source = INSTALLER.read_text(encoding="utf-8")

    assert '& $WorkflowPython -m pip install --disable-pip-version-check (Join-Path $EditableSkill "cli")' in source
    assert '& $WorkflowPython -c "import flask, jsonschema, PIL, pypdf, pypdfium2, docx, pptx, editppt"' in source


def test_verify_imports_only_the_installed_current_workflow_copy():
    """Verification must not hide a broken package by importing repository scripts."""
    source = VERIFY.read_text(encoding="utf-8")

    assert '$CurrentWorkflowRoot = Join-Path $RuntimeRoot $WorkflowPackageName' in source
    assert '$WorkflowScripts = Join-Path $CurrentWorkflowRoot "scripts"' in source
    assert '$WorkflowScripts = Join-Path $WorkflowSkill "scripts"' not in source
    assert '& $WorkflowPython (Join-Path $WorkflowScripts "doctor.py")' in source
    assert '& $WorkflowPython (Join-Path $WorkflowSkill "scripts\doctor.py")' not in source
    assert '$env:CODEX_GPT_IMAGE_SKILL = Join-Path $RuntimeRoot "generate-slide-body-image"' in source
    assert '& $EditablePython -c "import editppt, workflow_state, final_mechanical_qa;' in source
    assert 'import confirm_ui.server, workflow_state, final_mechanical_qa' not in source


def test_portable_verify_reuses_attested_v4_cli_result_without_relaunching_editable_python():
    source = VERIFY.read_text(encoding="utf-8")
    portable = source.split("if ($PortableSmokeTest) {", 1)[1].split("} else {", 1)[0]
    assert '$WorkflowPython -c "import editppt;' in portable
    assert "$EditablePython -c" not in portable
    assert '$Report.editppt_cli -ne "v4-build-validate-ok"' in source


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
    assert 'Join-Path $WorkflowStage "scripts\\runtime_office.py"' in installer


def test_runtime_packages_exact_image_generator_for_repair_attempts():
    installer = INSTALLER.read_text(encoding="utf-8")

    assert '$ImageSkill = Join-Path $PluginRoot "skills\\generate-slide-body-image"' in installer
    assert '$ImageSkillStage = Join-Path $RuntimeRoot ".generate-slide-body-image.$PID.tmp"' in installer
    assert 'Move-Item -LiteralPath $ImageSkillStage -Destination $CurrentImageSkillRoot' in installer
    assert '$env:CODEX_GPT_IMAGE_SKILL = $CurrentImageSkillRoot' in installer


def test_background_detector_uses_packaged_editppt_runtime_not_checkout_siblings(tmp_path: Path):
    """The copied Word workflow must find detector modules through the installed editppt package."""
    workflow_scripts = tmp_path / "workflow-current" / "scripts"
    shutil.copytree(WORD_WORKFLOW_SCRIPTS, workflow_scripts)
    blank = tmp_path / "blank.png"
    Image.new("RGB", (320, 180), "white").save(blank)

    probe = "\n".join([
        "import json, sys",
        f"sys.path.insert(0, {str(workflow_scripts)!r})",
        f"sys.path.insert(0, {str(EDITABLE_CLI_ROOT)!r})",
        "from background_text_detector import capability_status, detect_background_text",
        f"status = capability_status()",
        f"detection = detect_background_text(__import__('pathlib').Path({str(blank)!r})) if status['available'] else None",
        "print(json.dumps({'status': status, 'detection': detection}, ensure_ascii=False))",
    ])
    completed = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        cwd=tmp_path,
        timeout=30,
        check=False,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    result = __import__("json").loads(completed.stdout)
    assert result["status"]["available"] is True
    assert result["detection"]["background_text_detected"] is False


def test_v5_assembly_uses_packaged_editppt_runtime_without_checkout_siblings(tmp_path: Path):
    workflow_root = tmp_path / "workflow-current"
    workflow_scripts = workflow_root / "scripts"
    shutil.copytree(WORD_WORKFLOW_SCRIPTS, workflow_scripts)
    shutil.copytree(WORD_WORKFLOW_SCRIPTS.parent / "schemas", workflow_root / "schemas")
    completed = subprocess.run(
        [sys.executable, "-c", "import workflow_v5_assembly; print('assembly-import=ok')"],
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env={**os.environ, "PYTHONPATH": f"{workflow_scripts}{os.pathsep}{EDITABLE_CLI_ROOT}"},
        timeout=30,
        check=False,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "assembly-import=ok" in completed.stdout


def test_background_detector_runs_in_the_declared_editable_runtime(tmp_path: Path):
    """The Word runtime must use the one editable runtime that owns detector dependencies."""
    workflow_scripts = tmp_path / "workflow-current" / "scripts"
    shutil.copytree(WORD_WORKFLOW_SCRIPTS, workflow_scripts)
    blank = tmp_path / "blank.png"
    Image.new("RGB", (320, 180), "white").save(blank)

    editable_runtime = tmp_path / "editable-runtime"
    venv.EnvBuilder(with_pip=False, system_site_packages=True).create(editable_runtime)
    editable_python = editable_runtime / ("Scripts/python.exe" if platform.system() == "Windows" else "bin/python")
    site_packages = subprocess.run(
        [str(editable_python), "-c", "import site; print(site.getsitepackages()[0])"],
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    ).stdout.strip()
    (Path(site_packages) / "editppt_source.pth").write_text(str(EDITABLE_CLI_ROOT) + "\n", encoding="utf-8")

    probe = "\n".join([
        "import json, sys",
        f"sys.path.insert(0, {str(workflow_scripts)!r})",
        "from background_text_detector import capability_status, detect_background_text",
        "status = capability_status()",
        f"detection = detect_background_text(__import__('pathlib').Path({str(blank)!r})) if status['available'] else None",
        "print(json.dumps({'status': status, 'detection': detection}, ensure_ascii=False))",
    ])
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    environment["HOME"] = str(tmp_path / "isolated-home")
    environment["USERPROFILE"] = str(tmp_path / "isolated-home")
    environment.pop("EDITPPT_EXE", None)
    environment["EDITPPT_PYTHON"] = str(editable_python)
    completed = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=environment,
        timeout=60,
        check=False,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    result = __import__("json").loads(completed.stdout)
    assert result["status"]["available"] is True
    assert result["detection"]["background_text_detected"] is False
