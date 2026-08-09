from __future__ import annotations

from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
SKILLS = ROOT / "plugins" / "editable-ppt-workflow" / "skills"

EXPECTED = {
    "run-word-to-ppt-workflow": "Word 转可编辑 PPT 总流程",
    "generate-slide-body-image": "生成 PPT 页面主体图",
    "reconstruct-editable-slide": "重建可编辑 PPT 页面",
    "validate-ppt-output": "校验与修复 PPT 成品",
}

RETIRED = {
    "word-to-editable-ppt",
    "codex-gpt-image",
    "image-to-editable-ppt",
    "officecli",
}


def test_plugin_exposes_only_role_named_skills() -> None:
    discovered = {
        path.name
        for path in SKILLS.iterdir()
        if path.is_dir() and (path / "SKILL.md").is_file()
    }
    assert discovered == set(EXPECTED)
    assert not any((SKILLS / name).exists() for name in RETIRED)


def test_skill_frontmatter_and_ui_metadata_use_folder_name() -> None:
    for name, display_name in EXPECTED.items():
        skill_text = (SKILLS / name / "SKILL.md").read_text(encoding="utf-8")
        assert f"name: {name}\n" in skill_text

        metadata = yaml.safe_load(
            (SKILLS / name / "agents" / "openai.yaml").read_text(encoding="utf-8")
        )["interface"]
        assert metadata["display_name"] == display_name
        assert f"${name}" in metadata["default_prompt"]


def test_slide_body_generator_cannot_bypass_v4_authority() -> None:
    skill = (SKILLS / "generate-slide-body-image" / "SKILL.md").read_text(encoding="utf-8")
    assert "sealed page-generation work item" in skill
    assert "Do not use this role for standalone or general-purpose image generation" in skill
    assert "Never draw the fixed page title, actual SVG Logo, footer, footer line, or page number" in skill
    assert "abs((width / height) / (17 / 8) - 1) <= 0.01" in skill
    assert "Avoid adding logos, watermarks, or extra text unless requested" not in skill


def test_reconstruction_user_messages_use_role_name_but_keep_cli_compatibility() -> None:
    runtime = SKILLS / "reconstruct-editable-slide" / "cli" / "editppt" / "runtime"
    user_facing = "\n".join(
        (runtime / name).read_text(encoding="utf-8")
        for name in (
            "image_gen.py",
            "main.py",
            "prepare_deck_run.py",
            "remove_chroma_key.py",
            "runtime_env.py",
        )
    )
    assert "Install image-to-editable-ppt" not in user_facing
    assert "User-requested image-to-editable-ppt conversion" not in user_facing
    assert "reconstruct-editable-slide" in user_facing

    package = (SKILLS / "reconstruct-editable-slide" / "cli" / "pyproject.toml").read_text(encoding="utf-8")
    assert 'name = "image-to-editable-ppt-cli"' in package
