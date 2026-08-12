#!/usr/bin/env python3
"""Reject obsolete workflow surfaces from the installable plugin runtime."""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from pathlib import Path

from jsonschema.validators import validator_for


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
    "plugins/editable-ppt-workflow/skills/word-to-editable-ppt",
    "plugins/editable-ppt-workflow/skills/codex-gpt-image",
    "plugins/editable-ppt-workflow/skills/image-to-editable-ppt",
    "plugins/editable-ppt-workflow/skills/officecli",
    "plugins/editable-ppt-workflow/skills/reconstruct-editable-slide/cli/tests/test_v18_editable_cache.py",
)
PROHIBITED_RUNTIME_PATTERNS = {
    "retired regional skill name": re.compile(r"zhejiang[-_ ]ppt(?:[-_ ]v2)?|\bzjppt\b", re.IGNORECASE),
    "external PPT Master dependency": re.compile(r"ppt[-_]master", re.IGNORECASE),
    "historical workflow contract": re.compile(r"five[-_]master[-_]v(?:16|17|18)", re.IGNORECASE),
    "legacy approval/sample field": re.compile(r"master_approval|sample_status", re.IGNORECASE),
    "legacy visual-DNA field": re.compile(r"visual_dna|visual[- ]dna", re.IGNORECASE),
    "legacy page evidence": re.compile(
        r"five[-_ ]evidence|artifact_ownership|content_coverage|semantic_fidelity|information_structure|"
        r"page_context_receipt|relation_bindings",
        re.IGNORECASE,
    ),
    "legacy deck-wide visual QA": re.compile(
        r"global_qa|global[-_ ]visual[-_ ]qa|style_drift|cross[-_ ]page[-_ ]similarity",
        re.IGNORECASE,
    ),
    "legacy generated page category": re.compile(
        r"\b(?:content_)?master(?:s|_jobs|_visuals?|_image)?\b|\bsample(?:s|_image|_status)?\b",
        re.IGNORECASE,
    ),
}
V4_PRODUCTION_FILES = (
    "production_runner.py", "run_workflow.py", "workflow_state.py", "page_pipeline.py",
    "page_generation.py", "page_material_bundle_v4.py", "v4_qa.py", "v4_qa_gateway.py",
    "v4_reconstruction.py", "v4_reconstruction_gateway.py",
)
REMOVED_V4_SEMANTICS = {
    "removed native/hybrid route": re.compile(r"\b(?:native|hybrid)[-_ ]route\b|route\s*in\s*\{[^}]*['\"]native", re.IGNORECASE),
    "removed Image2 skip": re.compile(r"skip[-_ ]image2|image2[-_ ]skip", re.IGNORECASE),
    "removed background-only QA": re.compile(r"background[-_ ]only|text[-_ ]free[-_ ]background", re.IGNORECASE),
    "removed wrong-ratio acceptance": re.compile(r"16:9[-_ ]body|direct[-_ ]then[-_ ]centered[-_ ]contain", re.IGNORECASE),
    "removed flattened editability": re.compile(r"flattened[-_ ]editable|full[-_ ]body[-_ ]bitmap[-_ ]fallback", re.IGNORECASE),
}
ALLOWED_COMMANDS = frozenset({"confirm-ui", "doctor", "v6"})
REQUIRED_REQUIREMENTS = frozenset({"flask", "jsonschema", "pillow", "pypdf", "pypdfium2", "python-docx", "python-pptx"})
REQUIRED_V6_MODULES = frozenset({
    "workflow_v6_cli.py",
    "workflow_v6_materials.py",
    "workflow_v6_media.py",
    "workflow_v6_prompt_contract.py",
    "workflow_v6_image.py",
    "workflow_v6_qa.py",
    "workflow_v6_reconstruction.py",
    "workflow_v6_source.py",
    "workflow_v6_state.py",
})
REQUIRED_V6_SCHEMAS = frozenset({
    "page_materials_v6.schema.json",
    "reference_image_v6.schema.json",
    "style_confirmation.schema.json",
})


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


def _scan_v4_semantics(skill_root: Path, repo_root: Path) -> list[str]:
    entry = skill_root / "scripts" / "word_to_editable_ppt.py"
    cli = skill_root / "scripts" / "workflow_v6_cli.py"
    findings: list[str] = []
    for path in (entry, cli):
        if not path.is_file():
            findings.append(f"{_display(path, repo_root)}: required V6 production module is missing")
            continue
        text = path.read_text(encoding="utf-8-sig", errors="replace")
        if "workflow_v4" in text or "workflow_v5" in text:
            findings.append(f"{_display(path, repo_root)}: V6 production imports a legacy workflow")
    return findings


def _scan_other_plugin_runtime(repo_root: Path) -> list[str]:
    findings: list[str] = []
    plugin_root = repo_root / "plugins/editable-ppt-workflow"
    roots = (
        plugin_root / "skills/generate-slide-body-image/scripts",
        plugin_root / "skills/reconstruct-editable-slide/cli/editppt",
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


def _has_operation_input_guard(function: ast.AST, operation: str, *, require_inputs: bool) -> bool:
    """Recognize `operation == X and [not] image_inputs` guarding a raise."""
    for node in ast.walk(function):
        if not isinstance(node, ast.If) or not any(isinstance(item, ast.Raise) for item in ast.walk(node)):
            continue
        test = node.test
        if not isinstance(test, ast.BoolOp) or not isinstance(test.op, ast.And):
            continue
        has_operation = any(
            isinstance(item, ast.Compare)
            and any(isinstance(value, ast.Constant) and value.value == operation for value in item.comparators)
            for item in test.values
        )
        def field_name(value: ast.AST) -> str | None:
            if isinstance(value, ast.Name):
                return value.id
            if isinstance(value, ast.Attribute):
                return value.attr
            return None

        has_inputs = any(
            (require_inputs and field_name(item) in {"image_paths", "input_images"})
            or (
                not require_inputs
                and isinstance(item, ast.UnaryOp)
                and isinstance(item.op, ast.Not)
                and field_name(item.operand) in {"image_paths", "input_images"}
            )
            for item in test.values
        )
        if has_operation and has_inputs:
            return True
    return False


def _scan_initial_generation(skill_root: Path, repo_root: Path) -> list[str]:
    path = skill_root / "scripts/workflow_v6_image.py"
    if not path.is_file():
        return [f"{_display(path, repo_root)}: initial-generation builder is missing"]
    try:
        module = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
    except SyntaxError as error:
        return [f"{_display(path, repo_root)}:{error.lineno}: cannot parse initial-generation builder"]
    findings: list[str] = []
    functions = {node.name: node for node in module.body if isinstance(node, ast.FunctionDef)}
    request_builder = functions.get("build_image_request")
    command_builder = functions.get("build_image_command")
    if request_builder is None or command_builder is None:
        return [f"{_display(path, repo_root)}: adaptive Image2 request/command builder is missing"]

    selection_ok = any(
        isinstance(node, ast.keyword)
        and node.arg == "operation"
        and isinstance(node.value, ast.IfExp)
        and isinstance(node.value.test, ast.Name)
        and node.value.test.id == "images"
        and isinstance(node.value.body, ast.Constant)
        and node.value.body.value == "edit"
        and isinstance(node.value.orelse, ast.Constant)
        and node.value.orelse.value == "generate"
        for node in ast.walk(request_builder)
    )
    if not selection_ok:
        findings.append(f"{_display(path, repo_root)}: adaptive zero-reference generate / usable-reference edit selection is missing")

    command_literals = _literal_strings(command_builder)
    for required in ("generate", "edit", "--image", "--image-role", "--image-sha256"):
        if required not in command_literals:
            findings.append(f"{_display(path, repo_root)}: adaptive Image2 command is missing {required!r}")
    if not _has_operation_input_guard(command_builder, "edit", require_inputs=False):
        findings.append(f"{_display(path, repo_root)}: adaptive edit lacks an empty-input guard")
    if not _has_operation_input_guard(command_builder, "generate", require_inputs=True):
        findings.append(f"{_display(path, repo_root)}: adaptive generate lacks an image-input guard")
    return findings


def _scan_required_v6_files(skill_root: Path, repo_root: Path) -> list[str]:
    findings: list[str] = []
    for name in sorted(REQUIRED_V6_MODULES):
        path = skill_root / "scripts" / name
        if not path.is_file():
            findings.append(f"{_display(path, repo_root)}: required adaptive V6 module is missing")
    for name in sorted(REQUIRED_V6_SCHEMAS):
        path = skill_root / "schemas" / name
        if not path.is_file():
            findings.append(f"{_display(path, repo_root)}: required adaptive V6 schema is missing")
            continue
        try:
            schema = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError) as error:
            findings.append(f"{_display(path, repo_root)}: invalid JSON schema: {error}")
            continue
        object_shape = (
            schema.get("type") == "object" and isinstance(schema.get("properties"), dict)
        )
        union_shape = (
            isinstance(schema.get("$defs"), dict)
            and isinstance(schema.get("oneOf"), list)
            and bool(schema["oneOf"])
        )
        if not isinstance(schema, dict) or not (object_shape or union_shape):
            findings.append(f"{_display(path, repo_root)}: invalid JSON schema root shape")
            continue
        try:
            validator_for(schema).check_schema(schema)
        except Exception as error:  # jsonschema exposes draft-specific subclasses
            findings.append(f"{_display(path, repo_root)}: invalid JSON schema contract: {error}")
    return findings


def _scan_image_cli(plugin_root: Path, repo_root: Path) -> list[str]:
    path = plugin_root / "skills/generate-slide-body-image/scripts/codex_gpt_image.py"
    try:
        text = path.read_text(encoding="utf-8-sig")
        module = ast.parse(text, filename=str(path))
    except (OSError, SyntaxError) as error:
        return [f"{_display(path, repo_root)}: cannot inspect bundled Image2 CLI: {error}"]
    findings: list[str] = []
    functions = {node.name: node for node in module.body if isinstance(node, ast.FunctionDef)}
    builder = functions.get("build_parser")
    if builder is None:
        return [f"{_display(path, repo_root)}: bundled Image2 CLI lacks build_parser"]
    parser_names = {
        call.args[0].value
        for call in ast.walk(builder)
        if isinstance(call, ast.Call)
        and isinstance(call.func, ast.Attribute)
        and call.func.attr == "add_parser"
        and call.args
        and isinstance(call.args[0], ast.Constant)
        and isinstance(call.args[0].value, str)
    }
    for operation in ("generate", "edit"):
        if operation not in parser_names:
            findings.append(f"{_display(path, repo_root)}: bundled Image2 CLI lacks {operation!r} parser")
    builder_literals = _literal_strings(builder)
    for option in ("--image", "--image-sha256", "--image-role", "--prompt-file"):
        if option not in builder_literals:
            findings.append(f"{_display(path, repo_root)}: bundled Image2 CLI lacks {option!r} argument")
    if "cmd_generate" not in {
        node.id for node in ast.walk(builder) if isinstance(node, ast.Name)
    }:
        findings.append(f"{_display(path, repo_root)}: bundled Image2 parsers do not dispatch to cmd_generate")
    body_builder = functions.get("build_image_body")
    if body_builder is None:
        findings.append(f"{_display(path, repo_root)}: bundled Image2 CLI lacks build_image_body")
    else:
        if not _has_operation_input_guard(body_builder, "edit", require_inputs=False):
            findings.append(f"{_display(path, repo_root)}: bundled edit lacks empty-image guard")
        if not _has_operation_input_guard(body_builder, "generate", require_inputs=True):
            findings.append(f"{_display(path, repo_root)}: bundled generate lacks reference-image guard")
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
    skill_root = repo_root / "plugins/editable-ppt-workflow/skills/run-word-to-ppt-workflow"
    if not skill_root.is_dir():
        return [f"installable workflow skill is missing: {skill_root}"]
    findings: list[str] = []
    findings.extend(_scan_removed_paths(skill_root, repo_root))
    findings.extend(_scan_tokens(skill_root, repo_root))
    findings.extend(_scan_v4_semantics(skill_root, repo_root))
    findings.extend(_scan_other_plugin_runtime(repo_root))
    findings.extend(_scan_initial_generation(skill_root, repo_root))
    findings.extend(_scan_required_v6_files(skill_root, repo_root))
    findings.extend(_scan_image_cli(repo_root / "plugins/editable-ppt-workflow", repo_root))
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
