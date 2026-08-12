from pathlib import Path
import importlib.util
import sys


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import doctor


REPO_ROOT = Path(__file__).resolve().parents[5]
PLUGIN_ROOT = REPO_ROOT / "plugins" / "editable-ppt-workflow"
WORKFLOW = PLUGIN_ROOT / "skills" / "run-word-to-ppt-workflow"
GENERATOR = PLUGIN_ROOT / "skills" / "generate-slide-body-image"


def _load_runtime_checker():
    path = PLUGIN_ROOT / "scripts" / "check_current_runtime.py"
    spec = importlib.util.spec_from_file_location("check_current_runtime", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_auth_prefers_explicit_file(monkeypatch, tmp_path):
    selected = tmp_path / "explicit.json"
    monkeypatch.setenv("CODEX_AUTH_FILE", str(selected))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
    assert doctor.resolve_codex_auth_file() == selected


def test_auth_uses_codex_home_before_user_profile(monkeypatch, tmp_path):
    root = tmp_path / "codex-home"
    root.mkdir()
    selected = root / "auth.json"
    selected.write_text("{}", encoding="utf-8")
    monkeypatch.delenv("CODEX_AUTH_FILE", raising=False)
    monkeypatch.setenv("CODEX_HOME", str(root))
    assert doctor.resolve_codex_auth_file() == selected


def test_fake_ip_dns_is_reported_as_unavailable(monkeypatch):
    monkeypatch.setattr(doctor.socket, "getaddrinfo", lambda *_: [(2, 1, 6, "", ("198.18.0.1", 443))])
    result = doctor.codex_dns_status()
    assert result["fake_ip"] is True
    assert result["available"] is False


def test_active_workflow_documents_the_sealed_adaptive_v6_contract():
    text = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (
            WORKFLOW / "SKILL.md",
            WORKFLOW / "README.md",
            PLUGIN_ROOT / "README.md",
        )
    )
    required = (
        "new V6 project",
        "effective body",
        "attachment extraction",
        "failed_no_retry",
        "one Confirm UI",
        "thumbnail",
        "original",
        "model-input",
        "sealed result is the only prompt and QA authority",
        "zero valid confirmed references",
        "one to sixteen valid confirmed references",
        "never candidate 1",
        "medium",
        "high",
        "at most two candidates",
        "bounded concurrency",
        "original SVG Logo",
        "no post-generation exact overlay",
        "no post-reconstruction visual repair or comparison",
        "no V4/V5 runtime fallback",
    )
    for phrase in required:
        assert phrase in text


def test_active_skills_reject_obsolete_or_impossible_contracts():
    active = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (
            WORKFLOW / "SKILL.md",
            WORKFLOW / "README.md",
            GENERATOR / "SKILL.md",
            PLUGIN_ROOT / "README.md",
        )
    ).casefold()
    for obsolete in (
        "generate-only",
        "generate only",
        "reference-description-only",
        "pixel-perfect",
        "pixel perfect",
        "exact pixel fidelity",
        "post-generation exact overlay is required",
        "v4/v5 fallback",
    ):
        assert obsolete not in active


def test_runtime_checker_accepts_adaptive_cli_and_required_v6_modules():
    checker = _load_runtime_checker()
    assert checker.check(REPO_ROOT) == []
    assert checker.REQUIRED_V6_MODULES >= {
        "workflow_v6_materials.py",
        "workflow_v6_media.py",
        "workflow_v6_prompt_contract.py",
        "workflow_v6_image.py",
        "workflow_v6_qa.py",
    }
    assert checker.REQUIRED_V6_SCHEMAS >= {
        "page_materials_v6.schema.json",
        "reference_image_v6.schema.json",
    }


def test_runtime_checker_verifies_both_image_operations_and_input_guards():
    checker = _load_runtime_checker()
    findings = checker._scan_image_cli(PLUGIN_ROOT, REPO_ROOT)
    assert findings == []
