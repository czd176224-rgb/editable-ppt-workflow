"""Tests for the immutable style contract compiled from Confirm UI output."""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from jsonschema import Draft202012Validator
import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

from style_contract import (  # noqa: E402
    canonical_confirmation,
    canonical_json_bytes,
    compile_style_execution,
    freeze_style_contract,
)
from fixed_region_contract import fixed_frame_execution  # noqa: E402
import workflow_state  # noqa: E402
import style_contract  # noqa: E402
from current_contract_fixture import install_current_page_artifacts  # noqa: E402


def confirmed_result() -> dict:
    return {
        "stage": "final",
        "status": "confirmed",
        "confirmed_at": "2026-07-27T09:30:00+08:00",
        "canvas": "ppt169",
        "page_count": 2,
        "pagination_mode": "explicit_text_markers",
        "one_page_to_one_slide": True,
        "direction": 1,
        "template_selection": {
            "id": "policy-project-brief",
            "label": "政策与项目进度简报",
            "version": "1.0",
            "substyle_id": None,
            "override_fields": ["information_density"],
        },
        "visual_style": "editorial",
        "color": {
            "name_zh": "现代清晰",
            "palette": {
                "background": "#FFFFFF",
                "secondary_bg": "#F2F4F7",
                "primary": "#22577A",
                "accent": "#D97706",
                "secondary_accent": "#4B74A6",
                "body_text": "#1F2937",
            },
        },
        "icons": "tabler-outline",
        "typography": {
            "name_zh": "清晰双语无衬线",
            "heading": {"cjk": "Microsoft YaHei", "latin": "Arial", "css": "sans-serif"},
            "body": {"cjk": "Microsoft YaHei", "latin": "Arial", "css": "sans-serif"},
            "body_size": 24,
            "type_scale_pt": {"page_title": 28, "section_title": 18, "body": 12, "caption": 9},
        },
        "style_axes": {"formal": 80, "modern": 35, "minimal": 60},
        "layout_preferences": ["auto", "editorial", "matrix"],
        "information_density": "balanced",
        "regional_style": {"enabled": False},
        "background_system": "light",
        "image_role": {"role": "evidence", "proportion": "medium-low"},
        "evidence_strength": "data-case",
        "composition_tendency": "formal-consulting",
        "brand_device": "light",
        "production_profile": "balanced",
        "additional_requirements": "保持正式克制",
        "image_rendering": {
            "name_zh": "克制的平面表达",
            "rendering": "vector-illustration",
            "visual_zh": "平面矢量和几何构图",
            "mood_zh": "可信、克制",
        },
        "formula_policy": "mixed",
        "generation_mode": "continuous",
        "refine_spec": False,
        "image_quality": "high",
        "max_concurrency": 4,
        "automatic_repair_budget": 2,
        "editable_output": True,
        "start_generation": True,
    }


def _schema_errors(legacy_contract: dict, *, wrapped: bool):
    schema = json.loads((ROOT / "schemas" / "style_confirmation.schema.json").read_text(encoding="utf-8"))
    instance = legacy_contract
    if wrapped:
        instance = {
            "status": "confirmed",
            "revision": 1,
            "confirmed_at": "2026-08-12T00:00:00+08:00",
            "global_visual_contract": legacy_contract,
            "production_profile": "balanced",
            "confirmed_pages": [{
                "page_number": 1,
                "effective_body": "Approved body",
                "attachment_extracts": [],
                "chart_facts": [],
                "image_requirements": [],
                "degradations": [],
                "reference_images": [],
                "reference_decisions": [],
            }],
        }
    return list(Draft202012Validator(schema).iter_errors(instance))


@pytest.mark.parametrize("wrapped", [False, True], ids=["legacy", "v6-wrapper"])
@pytest.mark.parametrize(
    "field",
    ["template_selection", "typography", "style_axes", "regional_style", "image_role"],
)
def test_style_schema_rejects_empty_required_nested_contracts(field: str, wrapped: bool):
    """Replacing a detailed legacy definition with a bare object must remain invalid."""
    contract = confirmed_result()
    contract[field] = {}

    assert _schema_errors(contract, wrapped=wrapped)


@pytest.mark.parametrize("wrapped", [False, True], ids=["legacy", "v6-wrapper"])
def test_style_schema_rejects_arbitrary_or_duplicate_layout_preferences(wrapped: bool):
    """The legacy layout enum and uniqueness rules also govern V6's global contract."""
    for layouts in (["arbitrary-layout"], ["auto", "auto"]):
        contract = confirmed_result()
        contract["layout_preferences"] = layouts
        assert _schema_errors(contract, wrapped=wrapped)


@pytest.mark.parametrize("wrapped", [False, True], ids=["legacy", "v6-wrapper"])
def test_style_schema_narrowly_accepts_the_image_usage_policy(wrapped: bool):
    contract = confirmed_result()
    contract["image_usage_policy"] = "content-driven"

    assert not _schema_errors(contract, wrapped=wrapped)


def write_project(project: Path, confirmed: dict) -> None:
    (project / "confirm_ui").mkdir(parents=True)
    (project / "confirm_ui" / "result.json").write_text(
        json.dumps(confirmed, ensure_ascii=False), encoding="utf-8"
    )
    jobs = []
    for page_number in (1, 2):
        page_title = f"第{page_number}页当前结论"
        body_text = f"第{page_number}页当前正文"
        source_text = f"{page_title}\n{body_text}"
        contract = {
            "schema_version": "2.0",
            "page_number": page_number,
            "page_title": page_title,
            "body_text": body_text,
            "body_hash": hashlib.sha256(body_text.encode("utf-8")).hexdigest(),
            "source_text": source_text,
            "source_hash": hashlib.sha256(source_text.encode("utf-8")).hexdigest(),
        }
        contract_path = project / "01_page_contracts" / f"page_{page_number:03d}.json"
        contract_path.parent.mkdir(exist_ok=True)
        contract_path.write_text(json.dumps(contract, ensure_ascii=False), encoding="utf-8")
        job = {
            "slide_id": f"slide_{page_number:03d}",
            "page_number": page_number,
            "status": "pending_style_confirmation",
            "contract_file": f"01_page_contracts/page_{page_number:03d}.json",
            "expected_output": f"06_images/generated/page_{page_number:03d}.png",
        }
        jobs.append(install_current_page_artifacts(project, contract, job))
    logo = project / "00_source" / "company_logo.svg"
    logo.parent.mkdir(exist_ok=True)
    logo.write_text('<svg xmlns="http://www.w3.org/2000/svg" width="120" height="48"/>', encoding="utf-8")
    (project / "workflow_run.json").write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "workflow_contract_version": "word-ppt-workflow-v4",
                "project_name": "style-handoff",
                "word_source": {},
                "logo_source": {
                    "path": "00_source/company_logo.svg",
                    "sha256": hashlib.sha256(logo.read_bytes()).hexdigest(),
                    "media_type": "image/svg+xml",
                },
                "pagination": {"page_count": 2, "locked_page_order": [1, 2]},
                "style_confirmation": {"status": "pending", "confirmed_at": None},
                "jobs": jobs,
                "final_pptx": None,
            }
        ),
        encoding="utf-8",
    )


def test_compiled_contract_locks_one_shared_fixed_frame_and_keeps_body_creative():
    execution = compile_style_execution(confirmed_result())

    assert execution["canvas_profile"]["fit"] == "reconstruct_to_body"
    assert execution["canvas_profile"]["coordinate_space"] == "dynamic_source_normalized"
    assert "image_size" not in execution["canvas_profile"]
    assert execution["fixed_frame"] == {"title_color": "#22577A", **fixed_frame_execution()}
    assert execution["creative_freedom"]["layout"] is True
    assert execution["creative_freedom"]["composition"] is True


def test_compile_is_byte_deterministic_and_separates_locks_preferences_and_freedom():
    """The prompt contract must preserve choices without turning every choice into a lock."""
    first = compile_style_execution(confirmed_result())
    second = compile_style_execution(confirmed_result())

    assert canonical_json_bytes(first) == canonical_json_bytes(second)
    assert first == {
        "schema_version": "2.0",
        "canvas": "ppt169",
        "canvas_profile": {
            "aspect_ratio": "16:9",
            "slide_width_inches": 10.0,
            "slide_height_inches": 14.288 / 2.54,
            "fit": "reconstruct_to_body",
            "coordinate_space": "dynamic_source_normalized",
            "allow_crop": False,
        },
        "body_image_profile": {
            "version": "body-image-profile-v2",
            "production_profile": "balanced",
            "size": "1904x896",
            "ratio": "17:8",
            "mapping": "direct_then_repair",
            "direct_aspect_tolerance": 0.01,
        },
        "hard_constraints": {
            "content_fidelity": "preserve_information_and_logic",
            "one_page_to_one_slide": True,
            "title_color": "#22577A",
            "palette": confirmed_result()["color"]["palette"],
            "typography": confirmed_result()["typography"],
        },
        "fixed_frame": {"title_color": "#22577A", **fixed_frame_execution()},
        "soft_preferences": {
            "direction": 1,
            "template_selection": confirmed_result()["template_selection"],
            "visual_style": "editorial",
            "color": confirmed_result()["color"],
            "icons": "tabler-outline",
            "typography": confirmed_result()["typography"],
            "image_rendering": confirmed_result()["image_rendering"],
            "style_axes": {"formal": 80, "modern": 35, "minimal": 60},
            "layout_preferences": ["auto", "editorial", "matrix"],
            "information_density": "balanced",
            "regional_style": {"enabled": False},
            "background_system": "light",
            "image_role": {"role": "evidence", "proportion": "medium-low"},
            "evidence_strength": "data-case",
            "composition_tendency": "formal-consulting",
            "brand_device": "light",
            "additional_requirements": "保持正式克制",
        },
        "creative_freedom": {
            "layout": True,
            "composition": True,
            "visual_hierarchy": True,
            "content_visualization": True,
            "page_specific_emphasis": True,
        },
    }


def test_removed_stage1_fields_are_absent_and_page_content_cannot_change_hash():
    """Legacy fields and current-page facts are not style inputs."""
    baseline = compile_style_execution(confirmed_result())
    altered = confirmed_result()
    altered.update(
        {
            "communication_intent": "legacy",
            "audience_outcome": "legacy",
            "artifact_afterlife": "legacy",
            "page_text": "任意当前页内容",
            "pages": [{"page_number": 1, "text": "also arbitrary"}],
        }
    )

    compiled = compile_style_execution(altered)
    assert compiled == baseline
    assert not {"communication_intent", "audience_outcome", "artifact_afterlife", "page_text", "pages"} & compiled.keys()
    assert hashlib.sha256(canonical_json_bytes(compiled)).hexdigest() == hashlib.sha256(
        canonical_json_bytes(baseline)
    ).hexdigest()


def test_freeze_writes_canonical_artifacts_validates_schemas_and_advances_workflow(tmp_path: Path):
    """Freezing transfers the UI's final choice into the observable project state."""
    project = tmp_path / "project"
    write_project(project, confirmed_result())

    frozen = freeze_style_contract(project)
    style_dir = project / "02_style"
    confirmation_path = style_dir / "style_confirmation.json"
    execution_path = style_dir / "style_execution.json"
    hash_path = style_dir / "style_execution.sha256"
    visual_path = style_dir / "ui_preview_audit.png"
    visual_hash_path = style_dir / "ui_preview_audit.sha256"

    assert frozen["sha256"] == hash_path.read_text(encoding="ascii").strip()
    assert confirmation_path.read_bytes() == canonical_json_bytes(canonical_confirmation(confirmed_result()))
    assert execution_path.read_bytes() == canonical_json_bytes(frozen["execution"])
    assert frozen["sha256"] == hashlib.sha256(execution_path.read_bytes()).hexdigest()
    assert visual_path.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
    assert "approved_visual" not in frozen["execution"]
    assert hashlib.sha256(visual_path.read_bytes()).hexdigest() == visual_hash_path.read_text(encoding="ascii").strip()
    for name, instance in (("style_confirmation", confirmed_result()), ("style_execution", frozen["execution"])):
        schema = json.loads((ROOT / "schemas" / f"{name}.schema.json").read_text(encoding="utf-8"))
        assert not list(Draft202012Validator(schema).iter_errors(instance))

    state = json.loads((project / "workflow_run.json").read_text(encoding="utf-8"))
    assert state["style_confirmation"] == {
        "status": "confirmed",
        "confirmed_at": "2026-07-27T09:30:00+08:00",
        "confirmation_file": "02_style/style_confirmation.json",
        "execution_file": "02_style/style_execution.json",
        "execution_sha256": frozen["sha256"],
        "ui_preview_audit_file": "02_style/ui_preview_audit.png",
        "ui_preview_audit_sha256": visual_hash_path.read_text(encoding="ascii").strip(),
    }
    assert state["scheduler"] == {
        "concurrency": 4,
        "configured_max": 4,
        "last_trigger": "style_confirmation",
    }
    assert state["runtime"] == {
        "generation_mode": "continuous",
        "image_quality": "high",
        "automatic_repair_budget": 2,
    }
    report = json.loads((project / "09_reports" / "pipeline_metrics.json").read_text(encoding="utf-8"))
    assert report["state_revision"] == hashlib.sha256(
        (project / "workflow_run.json").read_bytes()
    ).hexdigest()
    assert [job["status"] for job in state["jobs"]] == [
        "queued",
        "queued",
    ]
    assert [job["complexity_weight"] for job in state["jobs"]] == [1, 1]


def test_repeated_and_concurrent_freeze_is_read_only_after_the_first_transition(tmp_path: Path):
    project = tmp_path / "project"
    write_project(project, confirmed_result())
    first = freeze_style_contract(project)
    state_path = project / "workflow_run.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["scheduler"] = {
        "concurrency": 2, "configured_max": 4, "last_trigger": "dispatch", "completed_rounds": 3,
    }
    state["runtime"]["production_marker"] = "do-not-reset"
    state["jobs"][0]["status"] = "generating"
    state_path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    tracked = [
        project / "02_style" / "style_confirmation.json",
        project / "02_style" / "style_execution.json",
        project / "02_style" / "style_execution.sha256",
        project / "02_style" / "ui_preview_audit.png",
        project / "02_style" / "ui_preview_audit.sha256",
    ]
    before = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in tracked}
    time.sleep(0.02)

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: freeze_style_contract(project), range(4)))

    after_state = json.loads(state_path.read_text(encoding="utf-8"))
    assert {item["sha256"] for item in results} == {first["sha256"]}
    assert after_state["scheduler"] == state["scheduler"]
    assert after_state["runtime"] == state["runtime"]
    assert after_state["jobs"][0]["status"] == "generating"
    assert {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in tracked} == before


def _revision_writer():
    writer = getattr(style_contract, "revise_style_contract", None)
    assert callable(writer), "style revisions require a dedicated production writer"
    return writer


def revised_result() -> dict:
    revised = confirmed_result()
    revised["confirmed_at"] = "2026-07-28T10:45:00+08:00"
    revised["color"] = {
        **revised["color"],
        "name_zh": "稳健深蓝",
        "palette": {**revised["color"]["palette"], "primary": "#123456"},
    }
    revised["additional_requirements"] = "重确认后的稳健深蓝视觉系统"
    return revised


def _expected_revision_artifacts(project: Path) -> list[tuple[Path, bytes]]:
    confirmed = canonical_confirmation(revised_result())
    execution = compile_style_execution(confirmed)
    title, body = style_contract._first_page_source(project)
    values = (
        ("style_confirmation", canonical_json_bytes(confirmed), ".json"),
        ("style_execution", canonical_json_bytes(execution), ".json"),
        ("ui_preview_audit", style_contract.render_ui_preview_audit(confirmed, title, body), ".png"),
    )
    versions = project / "02_style" / "versions"
    return [
        (versions / f"{name}_{hashlib.sha256(contents).hexdigest()}{suffix}", contents)
        for name, contents, suffix in values
    ]


def _inject_artifact_failure(
    patch: pytest.MonkeyPatch, stage: str, target: Path,
) -> None:
    real_open = os.open
    real_write = os.write
    real_fsync = os.fsync
    real_link = os.link
    target_descriptors: set[int] = set()
    partial_started = False

    def tracked_open(path, flags, mode=0o777):
        descriptor = real_open(path, flags, mode)
        if Path(path).name.startswith(f".{target.name}."):
            target_descriptors.add(descriptor)
        return descriptor

    patch.setattr(style_contract.os, "open", tracked_open)
    if stage == "partial_write":
        def injected_write(descriptor: int, data: bytes) -> int:
            nonlocal partial_started
            if descriptor not in target_descriptors:
                return real_write(descriptor, data)
            if not partial_started:
                partial_started = True
                return real_write(descriptor, data[:3])
            raise OSError("injected partial write failure")

        patch.setattr(style_contract.os, "write", injected_write)
    elif stage == "fsync":
        def injected_fsync(descriptor: int) -> None:
            if descriptor in target_descriptors:
                raise OSError("injected fsync failure")
            real_fsync(descriptor)

        patch.setattr(style_contract.os, "fsync", injected_fsync)
    else:
        def injected_link(source, destination) -> None:
            if Path(destination) == target:
                raise OSError("injected link failure")
            real_link(source, destination)

        patch.setattr(style_contract.os, "link", injected_link)


def test_revision_creates_content_addressed_artifacts_and_atomically_switches_only_the_gate(
    tmp_path: Path,
):
    project = tmp_path / "project"
    write_project(project, confirmed_result())
    first = freeze_style_contract(project)
    canonical_paths = [
        project / "02_style/style_confirmation.json",
        project / "02_style/style_execution.json",
        project / "02_style/style_execution.sha256",
        project / "02_style/ui_preview_audit.png",
        project / "02_style/ui_preview_audit.sha256",
    ]
    canonical_before = {path: path.read_bytes() for path in canonical_paths}
    state_before = json.loads((project / "workflow_run.json").read_text(encoding="utf-8"))

    revision = _revision_writer()(project, revised_result())

    state_after = json.loads((project / "workflow_run.json").read_text(encoding="utf-8"))
    gate = state_after["style_confirmation"]
    assert gate == revision["gate"]
    assert gate["execution_sha256"] != first["sha256"]
    assert gate["execution_file"] == (
        f"02_style/versions/style_execution_{gate['execution_sha256']}.json"
    )
    for path_key, sha_key in (
        ("confirmation_file", "confirmation_sha256"),
        ("execution_file", "execution_sha256"),
        ("ui_preview_audit_file", "ui_preview_audit_sha256"),
    ):
        artifact = project / gate[path_key]
        assert artifact.is_file() and not artifact.is_symlink()
        assert artifact.stat().st_nlink == 1
        assert hashlib.sha256(artifact.read_bytes()).hexdigest() == gate[sha_key]
    assert {path: path.read_bytes() for path in canonical_paths} == canonical_before
    assert state_after["jobs"] == state_before["jobs"]
    assert state_after["scheduler"] == state_before["scheduler"]
    assert state_after["runtime"] == state_before["runtime"]


def test_identical_style_revision_is_create_once_and_byte_idempotent(tmp_path: Path):
    project = tmp_path / "project"
    write_project(project, confirmed_result())
    freeze_style_contract(project)
    writer = _revision_writer()
    first = writer(project, revised_result())
    tracked = [project / first["gate"][field] for field in (
        "confirmation_file", "execution_file", "ui_preview_audit_file",
    )]
    before = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in tracked}
    state_before = (project / "workflow_run.json").read_bytes()

    second = writer(project, revised_result())

    assert second == first
    assert (project / "workflow_run.json").read_bytes() == state_before
    assert {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in tracked} == before


@pytest.mark.parametrize(
    ("field", "changed_value"),
    [
        ("max_concurrency", 3),
        ("generation_mode", "split"),
        ("image_quality", "medium"),
        ("automatic_repair_budget", 1),
    ],
)
def test_style_only_revision_rejects_runtime_or_scheduler_changes_without_state_mutation(
    tmp_path: Path, field: str, changed_value,
) -> None:
    project = tmp_path / field
    write_project(project, confirmed_result())
    freeze_style_contract(project)
    state_path = project / "workflow_run.json"
    state_before = state_path.read_bytes()
    revised = revised_result()
    revised[field] = changed_value

    with pytest.raises(ValueError, match="style-only revision"):
        _revision_writer()(project, revised)

    assert state_path.read_bytes() == state_before


@pytest.mark.parametrize("failure_stage", ["partial_write", "fsync", "link"])
@pytest.mark.parametrize("artifact_index", [0, 1, 2])
def test_revision_failure_rolls_back_only_new_final_artifacts_and_retry_succeeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    failure_stage: str, artifact_index: int,
) -> None:
    project = tmp_path / f"{failure_stage}-{artifact_index}"
    write_project(project, confirmed_result())
    freeze_style_contract(project)
    state_path = project / "workflow_run.json"
    state_before = state_path.read_bytes()
    versions = project / "02_style" / "versions"
    artifacts = _expected_revision_artifacts(project)

    with monkeypatch.context() as patch:
        _inject_artifact_failure(patch, failure_stage, artifacts[artifact_index][0])
        with pytest.raises(OSError, match="injected"):
            _revision_writer()(project, revised_result())

    assert state_path.read_bytes() == state_before
    assert all(not path.exists() for path, _contents in artifacts)
    assert list(versions.glob(".*.tmp")) == []

    revision = _revision_writer()(project, revised_result())
    assert state_path.read_bytes() != state_before
    assert all(
        (project / revision["gate"][field]).is_file()
        for field in ("confirmation_file", "execution_file", "ui_preview_audit_file")
    )
    assert list(versions.glob(".*.tmp")) == []


def test_revision_rollback_never_deletes_a_preexisting_identical_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "preexisting"
    write_project(project, confirmed_result())
    freeze_style_contract(project)
    state_path = project / "workflow_run.json"
    state_before = state_path.read_bytes()
    artifacts = _expected_revision_artifacts(project)
    preexisting_path, preexisting_bytes = artifacts[0]
    preexisting_path.parent.mkdir(parents=True)
    preexisting_path.write_bytes(preexisting_bytes)

    with monkeypatch.context() as patch:
        _inject_artifact_failure(patch, "link", artifacts[1][0])
        with pytest.raises(OSError, match="injected link failure"):
            _revision_writer()(project, revised_result())

    assert state_path.read_bytes() == state_before
    assert preexisting_path.read_bytes() == preexisting_bytes
    assert all(not path.exists() for path, _contents in artifacts[1:])
    assert list(preexisting_path.parent.glob(".*.tmp")) == []

    revision = _revision_writer()(project, revised_result())
    assert (project / revision["gate"]["confirmation_file"]).read_bytes() == preexisting_bytes


def test_revision_fails_closed_on_current_tamper_or_version_conflict(tmp_path: Path):
    for case in ("current-tamper", "version-conflict"):
        project = tmp_path / case
        write_project(project, confirmed_result())
        freeze_style_contract(project)
        gate_before = json.loads((project / "workflow_run.json").read_text(encoding="utf-8"))[
            "style_confirmation"
        ]
        if case == "current-tamper":
            (project / gate_before["execution_file"]).write_bytes(b"tampered\n")
        else:
            execution_bytes = canonical_json_bytes(compile_style_execution(revised_result()))
            digest = hashlib.sha256(execution_bytes).hexdigest()
            target = project / "02_style/versions" / f"style_execution_{digest}.json"
            target.parent.mkdir(parents=True)
            target.write_bytes(b"conflicting immutable bytes\n")

        with pytest.raises(ValueError):
            _revision_writer()(project, revised_result())

        assert json.loads((project / "workflow_run.json").read_text(encoding="utf-8"))[
            "style_confirmation"
        ] == gate_before


def test_revision_rejects_non_project_gate_paths_and_linked_version_targets(tmp_path: Path):
    project = tmp_path / "project"
    write_project(project, confirmed_result())
    freeze_style_contract(project)
    state_path = project / "workflow_run.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["style_confirmation"]["execution_file"] = "../outside-style.json"
    state_path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    gate_before = state["style_confirmation"]

    with pytest.raises(ValueError):
        _revision_writer()(project, revised_result())
    assert json.loads(state_path.read_text(encoding="utf-8"))["style_confirmation"] == gate_before

    # A content-addressed target with multiple names is not a create-once regular artifact.
    state["style_confirmation"]["execution_file"] = "02_style/style_execution.json"
    state_path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    execution_bytes = canonical_json_bytes(compile_style_execution(revised_result()))
    digest = hashlib.sha256(execution_bytes).hexdigest()
    versions = project / "02_style/versions"
    versions.mkdir(parents=True, exist_ok=True)
    source = versions / "attacker-owned.json"
    source.write_bytes(execution_bytes)
    target = versions / f"style_execution_{digest}.json"
    os.link(source, target)
    gate_before = json.loads(state_path.read_text(encoding="utf-8"))["style_confirmation"]

    with pytest.raises(ValueError):
        _revision_writer()(project, revised_result())
    assert json.loads(state_path.read_text(encoding="utf-8"))["style_confirmation"] == gate_before


def test_revision_write_failure_never_changes_the_live_gate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    project = tmp_path / "project"
    write_project(project, confirmed_result())
    freeze_style_contract(project)
    state_path = project / "workflow_run.json"
    state_before = state_path.read_bytes()

    def fail_write(_path: Path, _contents: bytes) -> None:
        raise OSError("injected create-once failure")

    monkeypatch.setattr(style_contract, "_create_once", fail_write, raising=False)
    with pytest.raises(OSError, match="injected"):
        _revision_writer()(project, revised_result())
    assert state_path.read_bytes() == state_before
