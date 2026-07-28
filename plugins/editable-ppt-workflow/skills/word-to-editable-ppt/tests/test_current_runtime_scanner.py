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


def test_skill_reconstruction_example_dispatches_attempt_two_before_record():
    skill = (REPO_ROOT / "plugins/editable-ppt-workflow/skills/word-to-editable-ppt/SKILL.md").read_text(encoding="utf-8")
    dispatch = "workflow dispatch --project D:\\Projects\\Deck --page 1 --agent page-1 --attempt 2"
    record = "editppt run record D:\\Projects\\Deck --page 1 --agent-id page-1 --attempt 2"

    assert dispatch in skill
    assert record in skill
    assert skill.index(dispatch) < skill.index(record)
