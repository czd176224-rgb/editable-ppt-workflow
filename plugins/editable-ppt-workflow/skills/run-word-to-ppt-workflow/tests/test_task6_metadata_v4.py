from __future__ import annotations

import json
from pathlib import Path


REPO = Path(__file__).resolve().parents[5]
PLUGIN = REPO / "plugins/editable-ppt-workflow"


def test_release_metadata_is_consistently_v5_version() -> None:
    package = json.loads((REPO / "package-info.json").read_text(encoding="utf-8"))
    manifest = json.loads((PLUGIN / ".codex-plugin/plugin.json").read_text(encoding="utf-8"))
    marketplace = json.loads((REPO / ".agents/plugins/marketplace.json").read_text(encoding="utf-8"))
    assert package["pluginVersion"] == manifest["version"] == "1.2.0"
    assert package["releaseTag"] == "v1.2.0"
    assert package["promptContractVersion"] == "page-prompt-v8"
    assert package["qaPolicyVersion"] == "risk-qa-v5"
    assert package["apiKeyRequired"] is False
    expected_marketplace = (
        "editable-ppt-public"
        if package["repositoryVisibility"] == "public"
        else "editable-ppt-local-preview-v110"
    )
    assert package["marketplacePreviewIdentity"] == marketplace["name"] == expected_marketplace
    assert marketplace["interface"]["displayName"].endswith("1.2.0")
    assert package["workflowContractVersion"] == "word-ppt-workflow-v5"
    assert package["bodyImageAspectPolicy"] == "17:8-relative-error-at-most-0.01"
    assert package["everyPageCallsImage2"] is True
    assert package["reconstructionPolicy"] == "sealed-composed-body-editppt-single-authority-high-fidelity-object-level-editable"
    assert package["designAcceptancePolicy"] == (
        "shared-authentic-slots-then-exact-source-compose-and-semantic-acceptance-with-one-targeted-repair"
    )
    assert package["qaPolicy"].startswith("accepted-composed-body-vs-final-body-crop-v7")


def test_active_v5_docs_do_not_advertise_removed_production_semantics() -> None:
    paths = [
        REPO / "README.md", PLUGIN / "README.md",
        PLUGIN / "skills/run-word-to-ppt-workflow/README.md",
        PLUGIN / "skills/run-word-to-ppt-workflow/SKILL.md",
        PLUGIN / "skills/run-word-to-ppt-workflow/template/README.md",
    ]
    banned = ("word-ppt-workflow-v3", "image/native/hybrid", "direct/contain", "background-only", "无文字视觉层")
    for path in paths:
        text = path.read_text(encoding="utf-8")
        assert "word-ppt-workflow-v5" in text, path
        assert "17:8" in text and "1%" in text, path
        assert all(token not in text for token in banned), path


def test_subscription_runtime_has_no_api_key_or_openai_sdk_prerequisite() -> None:
    doctor = (PLUGIN / "skills/run-word-to-ppt-workflow/scripts/doctor.py").read_text(encoding="utf-8")
    requirements = (PLUGIN / "skills/run-word-to-ppt-workflow/requirements.txt").read_text(encoding="utf-8")
    pyproject = (PLUGIN / "skills/reconstruct-editable-slide/cli/pyproject.toml").read_text(encoding="utf-8")
    assert '"api_key_required": False' in doctor
    assert "codex-app-server" in doctor
    assert not any(line.strip().lower().startswith("openai") for line in requirements.splitlines())
    assert '"openai' not in pyproject.lower() and "'openai" not in pyproject.lower()
