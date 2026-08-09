"""Regression tests for removal of legacy template surfaces."""

from __future__ import annotations

import importlib.util
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[5]
SCANNER_PATH = REPO_ROOT / "plugins/editable-ppt-workflow/scripts/check_current_runtime.py"
SPEC = importlib.util.spec_from_file_location("check_current_runtime", SCANNER_PATH)
assert SPEC and SPEC.loader
scanner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(scanner)


def test_scanner_reads_template_gitignore(tmp_path: Path):
    skill_root = tmp_path / "skill"
    template = skill_root / "template"
    template.mkdir(parents=True)
    gitignore = template / ".gitignore"
    gitignore.write_text("04_assets/master_visuals/*\n", encoding="utf-8")

    files = scanner._runtime_text_files(skill_root)
    findings = scanner._scan_tokens(skill_root, tmp_path)

    assert gitignore in files
    assert any("legacy generated page category" in finding for finding in findings)


def test_scanner_rejects_both_removed_legacy_template_directories(tmp_path: Path):
    skill_root = tmp_path / "skill"
    (skill_root / "template/04_assets/master_visuals").mkdir(parents=True)
    (skill_root / "template/07_editable/master").mkdir(parents=True)

    findings = scanner._scan_removed_paths(skill_root, tmp_path)

    assert any("template/04_assets/master_visuals" in finding for finding in findings)
    assert any("template/07_editable/master" in finding for finding in findings)


def test_scanner_rejects_obsolete_image_and_delivery_directories(tmp_path: Path):
    skill_root = tmp_path / "skill"
    for relative in ("template/06_images/approved", "template/06_images/draft", "template/09_deliverables"):
        (skill_root / relative).mkdir(parents=True)

    findings = scanner._scan_removed_paths(skill_root, tmp_path)

    assert any("template/06_images/approved" in finding for finding in findings)
    assert any("template/06_images/draft" in finding for finding in findings)
    assert any("template/09_deliverables" in finding for finding in findings)


def test_scanner_rejects_retired_regional_skill_name(tmp_path: Path):
    skill_root = tmp_path / "skill"
    scripts = skill_root / "scripts"
    scripts.mkdir(parents=True)
    (scripts / "entry.py").write_text('name = "zhejiang-ppt-v2"\n', encoding="utf-8")

    findings = scanner._scan_tokens(skill_root, tmp_path)

    assert any("retired regional skill name" in finding for finding in findings)


def test_skill_documents_one_command_signed_reconstruction_without_manual_state_bypass():
    skill = (REPO_ROOT / "plugins/editable-ppt-workflow/skills/run-word-to-ppt-workflow/SKILL.md").read_text(encoding="utf-8")
    assert "word_to_editable_ppt.py run" in skill
    assert "signed visual reconstruction gateway" in skill
    assert "Never hand-edit receipts or state" in skill
    assert "editppt run record" not in skill


def test_current_command_registry_matches_runtime_policy():
    findings = scanner._scan_commands(
        REPO_ROOT / "plugins/editable-ppt-workflow/skills/run-word-to-ppt-workflow",
        REPO_ROOT,
    )

    assert findings == []
