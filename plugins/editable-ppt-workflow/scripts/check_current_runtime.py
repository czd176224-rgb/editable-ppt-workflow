#!/usr/bin/env python3
"""Reject obsolete workflow surfaces from the installable plugin runtime."""

from __future__ import annotations

import argparse
import ast
import re
import sys
from pathlib import Path


TEXT_SUFFIXES = frozenset({".json", ".md", ".py", ".ps1", ".sh"})
PROHIBITED_PATHS = (
    "schemas/artifact_ownership.schema.json",
    "schemas/asset_manifest.schema.json",
    "schemas/information_structure.schema.json",
    "schemas/page_context_receipt.schema.json",
    "schemas/visual_dna_receipt.schema.json",
    "scripts/asset_coverage.py",
    "scripts/audit_assets.py",
    "scripts/audit_content_coverage.py",
    "scripts/audit_pptx.py",
    "scripts/content_attempt_qa.py",
    "scripts/dense_editorial_fallback.py",
    "scripts/final_preview.py",
    "scripts/page_context.py",
    "scripts/performance_metrics.py",
    "scripts/qa_planner.py",
    "scripts/recovery.py",
    "scripts/style_lock.py",
    "scripts/validate_project.py",
    "scripts/visual_dna.py",
    "scripts/visual_qa.py",
    "template/04_assets/master_visuals",
    "template/07_editable/master",
    "template/06_images/approved",
    "template/06_images/draft",
    "template/09_deliverables",
    "tests/fixtures/real_ocr_stub.py",
    "tests/test_assets.py",
    "tests/test_codex_gpt_image.py",
    "tests/test_dense_editorial_fallback.py",
    "tests/test_portable_workflow.py",
    "tests/test_source_assets.py",
    "tests/test_v18_cache.py",
    "tests/test_v18_contract.py",
    "tests/test_v18_page_pipeline.py",
    "tests/test_v18_qa_planner.py",
    "tests/test_v18_scheduler.py",
    "tests/test_visual_qa.py",
)
PROHIBITED_REPO_PATHS = (
    "plugins/editable-ppt-workflow/skills/zhejiang-ppt-v2",
    "plugins/editable-ppt-workflow/skills/image-to-editable-ppt/cli/tests/test_v18_editable_cache.py",
)
PROHIBITED_RUNTIME_PATTERNS = {
    "retired regional skill name": re.compile(r"zhejiang[-_ ]ppt(?:[-_ ]v2)?|\bzjppt\b", re.IGNORECASE),
    "external PPT Master dependency": re.compile(r"ppt[-_]master", re.IGNORECASE),
    "historical workflow contract": re.compile(r"five[-_]master[-_]v(?:16|17|18)", re.IGNORECASE),
    "legacy approval/sample field": re.compile(r"master_approval|sample_status", re.IGNORECASE),
    "legacy uploaded logo field": re.compile(r"company_logo|\blogo(?:s)?\b", re.IGNORECASE),
    "legacy style-image field": re.compile(r"style_reference|style[-_ ]image", re.IGNORECASE),
    "legacy visual-DNA field": re.compile(r"visual_dna|visual[- ]dna", re.IGNORECASE),
    "legacy page evidence": re.compile(
        r"five[-_ ]evidence|artifact_ownership|content_coverage|semantic_fidelity|information_structure|"
        r"page_context_receipt|relation_bindings|generation_trace",
        re.IGNORECASE,
    ),
    "legacy deck-wide visual QA": re.compile(
        r"global_qa|global[-_ ]visual|style_drift|cross[-_ ]page[-_ ]similarity",
        re.IGNORECASE,
    ),
    "legacy generated page category": re.compile(
        r"\b(?:content_)?master(?:s|_jobs|_visuals?|_image)?\b|\bsample(?:s|_image|_status)?\b",
        re.IGNORECASE,
    ),
}
ALLOWED_COMMANDS = frozenset({"confirm-ui", "doctor", "prepare", "workflow"})
REQUIRED_REQUIREMENTS = frozenset({"flask", "jsonschema", "pillow", "pymupdf", "python-docx", "python-pptx"})


def _display(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return str(path)


def _runtime_text_files(skill_root: Path) -> list[Path]:
    files: list[Path] = []
    for relative in ("scripts", "schemas", "template"):
        base = skill_root / relative
        if not base.is_dir():
            continue
        files.extend(
            path
            for path in base.rglob("*")
            if path.is_file()
            and (path.suffix.lower() in TEXT_SUFFIXES or path.name == ".gitignore")
        )
    return sorted(files)


def _scan_tokens(skill_root: Path, repo_root: Path) -> list[str]:
    findings: list[str] = []
    for path in _runtime_text_files(skill_root):
        text = path.read_text(encoding="utf-8-sig", errors="replace")
        for line_number, line in enumerate(text.splitlines(), start=1):
            for label, pattern in PROHIBITED_RUNTIME_PATTERNS.items():
                if pattern.search(line):
                    findings.append(
                        f"{_display(path, repo_root)}:{line_number}: {label}: {line.strip()}"
                    )
    return findings


def _scan_other_plugin_runtime(repo_root: Path) -> list[str]:
    findings: list[str] = []
    plugin_root = repo_root / "plugins/editable-ppt-workflow"
    roots = (
        plugin_root / "skills/codex-gpt-image/scripts",
        plugin_root / "skills/image-to-editable-ppt/cli/editppt",
    )
    patterns = {
        "external PPT Master dependency": re.compile(r"ppt[-_]master", re.IGNORECASE),
        "historical workflow contract": re.compile(r"five[-_]master[-_]v(?:16|17|18)", re.IGNORECASE),
    }
    for root in roots:
        if not root.is_dir():
            findings.append(f"{_display(root, repo_root)}: required plugin runtime directory is missing")
            continue
        for path in sorted(root.rglob("*.py")):
            text = path.read_text(encoding="utf-8-sig", errors="replace")
            for line_number, line in enumerate(text.splitlines(), start=1):
                for label, pattern in patterns.items():
                    if pattern.search(line):
                        findings.append(
                            f"{_display(path, repo_root)}:{line_number}: {label}: {line.strip()}"
                        )
    return findings


def _scan_removed_paths(skill_root: Path, repo_root: Path) -> list[str]:
    findings = [
        f"{_display(skill_root / relative, repo_root)}: obsolete runtime path still exists"
        for relative in PROHIBITED_PATHS
        if (skill_root / relative).exists()
    ]
    findings.extend(
        f"{relative}: obsolete runtime path still exists"
        for relative in PROHIBITED_REPO_PATHS
        if (repo_root / relative).exists()
    )
    return findings


def _literal_strings(node: ast.AST) -> set[str]:
    return {
        item.value
        for item in ast.walk(node)
        if isinstance(item, ast.Constant) and isinstance(item.value, str)
    }


def _scan_initial_generation(skill_root: Path, repo_root: Path) -> list[str]:
    path = skill_root / "scripts/page_generation.py"
    if not path.is_file():
        return [f"{_display(path, repo_root)}: initial-generation builder is missing"]
    try:
        module = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
    except SyntaxError as error:
        return [f"{_display(path, repo_root)}:{error.lineno}: cannot parse initial-generation builder"]
    functions = {
        node.name: node
        for node in module.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    initial = functions.get("build_initial_request")
    if initial is None:
        return [f"{_display(path, repo_root)}: build_initial_request is missing"]
    literals = _literal_strings(initial)
    findings: list[str] = []
    if "generate" not in literals:
        findings.append(
            f"{_display(path, repo_root)}:{initial.lineno}: initial generation must select operation=generate"
        )
    if "edit" in literals or "images/edits" in literals:
        findings.append(
            f"{_display(path, repo_root)}:{initial.lineno}: images/edits is forbidden for initial generation"
        )
    return findings


def _scan_commands(skill_root: Path, repo_root: Path) -> list[str]:
    path = skill_root / "scripts/word_to_editable_ppt.py"
    try:
        module = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
    except (OSError, SyntaxError) as error:
        return [f"{_display(path, repo_root)}: cannot inspect command registry: {error}"]
    commands: set[str] | None = None
    for node in module.body:
        if not isinstance(node, ast.Assign):
            continue
        if not any(isinstance(target, ast.Name) and target.id == "TOOLS" for target in node.targets):
            continue
        if isinstance(node.value, ast.Dict):
            keys = [key.value for key in node.value.keys if isinstance(key, ast.Constant)]
            if len(keys) == len(node.value.keys) and all(isinstance(key, str) for key in keys):
                commands = set(keys)
        break
    if commands is None:
        return [f"{_display(path, repo_root)}: TOOLS command registry must be a literal mapping"]
    if commands != ALLOWED_COMMANDS:
        extra = sorted(commands - ALLOWED_COMMANDS)
        missing = sorted(ALLOWED_COMMANDS - commands)
        return [
            f"{_display(path, repo_root)}: command registry mismatch; "
            f"extra={extra}, missing={missing}"
        ]
    return []


def _scan_requirements(skill_root: Path, repo_root: Path) -> list[str]:
    path = skill_root / "requirements.txt"
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except OSError as error:
        return [f"{_display(path, repo_root)}: cannot read runtime prerequisites: {error}"]
    names = {
        re.split(r"[<=>!~\[;\s]", line.strip(), maxsplit=1)[0].lower()
        for line in lines
        if line.strip() and not line.lstrip().startswith(("#", "-"))
    }
    missing = sorted(REQUIRED_REQUIREMENTS - names)
    return (
        [f"{_display(path, repo_root)}: missing runtime prerequisites: {missing}"]
        if missing
        else []
    )


def check(repo_root: Path) -> list[str]:
    repo_root = repo_root.resolve()
    skill_root = repo_root / "plugins/editable-ppt-workflow/skills/word-to-editable-ppt"
    if not skill_root.is_dir():
        return [f"installable workflow skill is missing: {skill_root}"]
    findings: list[str] = []
    findings.extend(_scan_removed_paths(skill_root, repo_root))
    findings.extend(_scan_tokens(skill_root, repo_root))
    findings.extend(_scan_other_plugin_runtime(repo_root))
    findings.extend(_scan_initial_generation(skill_root, repo_root))
    findings.extend(_scan_commands(skill_root, repo_root))
    findings.extend(_scan_requirements(skill_root, repo_root))
    return findings


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=Path(__file__).resolve().parents[3],
        help="Repository root containing plugins/editable-ppt-workflow.",
    )
    args = parser.parse_args()
    findings = check(args.repo_root)
    if findings:
        print("Current-only runtime policy failed:", file=sys.stderr)
        for finding in findings:
            print(f"- {finding}", file=sys.stderr)
        return 1
    print("Current-only runtime policy passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
