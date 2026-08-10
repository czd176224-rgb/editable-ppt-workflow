from __future__ import annotations

import copy
import hashlib
import json
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Mapping

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from cache_key import CacheKeyInputs, build_page_cache_key  # noqa: E402
from adaptive_scheduler import AdaptiveScheduler, RoundOutcome  # noqa: E402
from cache_store import CacheStore  # noqa: E402
from page_material_bundle_v4 import (  # noqa: E402
    _BUNDLE_ATTESTATION_PURPOSE,
    _bundle_attestation_payload,
    _decode_text_evidence,
    _material_summary,
    _seal_digest,
)
from codex_web_material_gateway import sign_project_payload  # noqa: E402
from effective_page_authority import (  # noqa: E402
    _seal_digest as _authority_seal_digest,
    build_effective_page_authority,
)
from page_qa import PageQAResult, qa_issue  # noqa: E402
from page_coverage import build_coverage_contract, coverage_sha256  # noqa: E402
from style_contract import canonical_json_bytes  # noqa: E402
from fixed_region_contract import fixed_frame_execution  # noqa: E402
from page_requirement_summary import (  # noqa: E402
    _seal as _resolution_seal,
    _seal_page_entry,
    _sign_page_entry,
    _signature_payload as _resolution_signature_payload,
    build_page_requirement_summary,
    load_verified_page_resolutions,
)
import workflow_state  # noqa: E402
import v4_reconstruction  # noqa: E402
from workflow_state import (  # noqa: E402
    dispatch,
    load,
    next_action,
    record_generation,
    record_qa as _record_qa,
    record_editable_page as _record_editable_page,
    release_blocked_page,
    retry_search,
    resume,
    status,
)
from current_contract_fixture import (  # noqa: E402
    refresh_current_page_artifacts,
    write_valid_generation_receipt,
    write_valid_qa_observation,
)
from test_v4_editable_reconstruction import (  # noqa: E402
    build_and_sign_reconstruction as _build_signed_reconstruction,
)
from v4_reconstruction import (  # noqa: E402
    collect_reconstruction_closure,
    restore_and_validate_completed_cache,
    write_editable_receipt,
)
from editppt.runtime.fixed_region_runtime import CONTENT_BOX, SLIDE  # noqa: E402


def record_qa(project: Path, page: int, agent: str, attempt: int, result: PageQAResult):
    job = next(item for item in load(project)["jobs"] if item["page_number"] == page)
    if job["status"] != "qa":
        return _record_qa(project, page, agent, attempt, signed_invocation_bundle=None)
    failed_check = None
    detail = "The visual check failed."
    if result.status == "repair":
        failed_check = "key_facts_preserved" if result.repair_scope == "structural" else "readable_no_overflow"
        detail = result.issues[0]["message"]
    invocation = write_valid_qa_observation(
        project, page, failed_check=failed_check, failure_detail=detail,
    )
    return _record_qa(project, page, agent, attempt, signed_invocation_bundle=invocation)


def test_qa_policy_v4_migration_preserves_generation_but_invalidates_qa_and_downstream(tmp_path: Path) -> None:
    project = _project(tmp_path, page_count=1)
    state_path = project / "workflow_run.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    job = state["jobs"][0]
    job.update({
        "status": "complete",
        "qa_result": {"status": "pass", "issues": []},
        "qa_receipt": {"path": "old-qa.json", "sha256": "0" * 64},
        "reconstruction_work_item": {"path": "old-work.json", "sha256": "0" * 64},
        "editable_receipt": {"path": "old-editable.json", "sha256": "0" * 64},
        "editable_page": {"path": "old-page.pptx", "sha256": "0" * 64},
        "generation": {"image": "03_generated/page_001.png", "sha256": "1" * 64},
    })
    state["qa_policy_version"] = "risk-qa-v4"
    state_path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")

    migrated = load(project)
    migrated_job = migrated["jobs"][0]

    assert migrated["qa_policy_version"] == "risk-qa-v5"
    assert migrated_job["status"] == "qa"
    assert migrated_job["generation"]["image"] == "03_generated/page_001.png"
    assert "qa_receipt" not in migrated_job
    assert "editable_page" not in migrated_job


def _write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _issue(message: str, severity: str = "local") -> dict:
    return qa_issue(
        f"test_{severity}_issue", message, severity, "test_observation", "test_fixture",
    )


def _background_text_issue() -> dict:
    return qa_issue(
        "background_text_detected", "生成背景包含文字。", "local",
        "background_text_scan", "test_fixture",
    )


def _verified_receipt(project: Path, page_number: int) -> Path:
    job = next(item for item in load(project)["jobs"] if item["page_number"] == page_number)
    coverage = json.loads((project / job["coverage_contract_file"]).read_text(encoding="utf-8"))
    receipts = []
    for index, item in enumerate(coverage.get("required_items", []), start=1):
        expected = item["expected"]
        receipt = {
            "coverage_id": item["coverage_id"],
            "object_id": f"test-object-{index:03d}",
            "visible": True,
            "expected_sha256": expected["sha256"],
        }
        if expected.get("kind") == "asset_id":
            receipt["observed_asset_id"] = expected["value"]
        else:
            receipt["observed_text"] = expected["value"]
        receipts.append(receipt)
    path = project / "07_editable" / f"page_{page_number:03d}_body.coverage.json"
    _write_json(path, {"schema_version": "1.0", "receipts": receipts})
    return path


def record_editable_page(
    project: Path, page_number: int, agent: str, attempt: int, artifact: Path,
):
    return _record_editable_page(
        project, page_number, agent, attempt, artifact,
        editable_receipt=_verified_receipt(project, page_number),
    )


def _complete_real_reconstruction(project: Path, page_number: int, attempt: int) -> None:
    run = load(project)
    job = next(item for item in run["jobs"] if item["page_number"] == page_number)
    workflow_state._ensure_reconstruction_work_item(project, run, job)
    _write_json(project / "workflow_run.json", run)
    job = _job(project, page_number)
    work_path = project / job["reconstruction_work_item"]["path"]
    work = json.loads(work_path.read_text(encoding="utf-8"))
    text_boxes = []
    text_coverage = []
    for index, item in enumerate(work["authoritative_text"], start=1):
        object_name = f"body-paragraph-{index}"
        text_boxes.append({
            "object_id": item["source_id"], "name": object_name, "text": item["text"],
            "font_size": 18, "box_px": [2, 2 + (index - 1) * 6, 30, 5],
        })
        text_coverage.append({
            "source_id": item["source_id"], "text": item["text"],
            "object_name": object_name,
        })
    manifest = project / "07_editable" / f"page_{page_number:03d}" / "manifest.json"
    _write_json(manifest, {
        "artifact_version": "editable-reconstruction-manifest-v1",
        "work_item_sha256": hashlib.sha256(work_path.read_bytes()).hexdigest(),
        "workflow_contract_version": "fixed-canvas-cm-v2",
        "reconstruction_contract_version": "editable-image-v3",
        "slide": dict(SLIDE), "content_box": dict(CONTENT_BOX),
        "source": {"width_px": 34, "height_px": 16},
        "text_boxes": text_boxes, "tables": [],
        "shapes": [{
            "object_id": "panel-1", "name": "body-panel", "type": "rect",
            "box_px": [0, 0, 34, 16], "fill": "#f5f5f5", "z_index": 0,
        }],
        "images": [], "raster_components": [],
        "text_coverage": text_coverage, "table_coverage": [],
    })
    directory = manifest.parent
    bundle = _build_signed_reconstruction(
        project, work_item=work_path, manifest=manifest,
        body_pptx=directory / "body.pptx", final_pptx=directory / "page.pptx",
        bundle_output=directory / "signed-reconstruction.json",
    )
    receipt = write_editable_receipt(
        project, work_path, bundle, directory / "editable-receipt.json",
    )
    _record_editable_page(
        project, page_number, "reconstructor", attempt, directory / "page.pptx",
        editable_receipt=receipt,
    )


def test_confirmed_max_concurrency_caps_adaptive_growth():
    scheduler = AdaptiveScheduler(20, initial_concurrency=2, maximum_concurrency=2)

    snapshot = scheduler.record_round(RoundOutcome(successes=1, completed=1, expected=1))

    assert snapshot.concurrency == 2


def _project(
    tmp_path: Path,
    page_count: int = 3,
    *,
    confirmed: bool = True,
    generation_mode: str = "continuous",
    max_concurrency: int = 2,
) -> Path:
    project = tmp_path / "project"
    project.mkdir()
    jobs = []
    for page in range(1, page_count + 1):
        page_title = f"第{page}页结论"
        body_text = f"第{page}页的唯一内容"
        text = f"{page_title}\n{body_text}"
        contract = {
            "schema_version": "2.0",
            "workflow_contract_version": "word-ppt-workflow-v4",
            "page_number": page,
            "page_title": page_title,
            "title_origin": "explicit_word_heading",
            "body_text": body_text,
            "body_hash": hashlib.sha256(body_text.encode("utf-8")).hexdigest(),
            "source_text": text,
            "source_hash": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            "source_blocks": [{
                "type": "paragraph", "text": text, "source_block_index": 1,
                "relationship_ids": [], "comment_ids": [],
            }],
            "page_comments": [],
            "requirement_precedence": [
                "fixed_hard_rules", "ui_global_soft_preferences",
                "model_creative_freedom", "word_page_comments",
            ],
            "semantic_units": [{
                "unit_id": "unit_001", "kind": "sentence", "text": body_text,
                "source_block_index": 1,
                "source_sha256": hashlib.sha256(body_text.encode("utf-8")).hexdigest(),
                "source_trace": [{
                    "source_type": "word_page", "source_page": page,
                    "source_locator": "paragraph:1", "text_span": body_text,
                    "excerpt": body_text,
                }],
            }],
            "source_tables": [],
            "explicit_relations": [],
            "relationship_contract_sha256": hashlib.sha256(b"[]").hexdigest(),
            "asset_bindings": [],
            "must_keep": [body_text],
            "key_facts": [],
            "key_data": [],
            "forbidden_cross_page_content": [],
            "page_purpose": "test page",
            "detected_dates": [],
            "detected_numbers": [],
            "detected_amounts": [],
            "human_review_status": "pending",
            "human_review_notes": [],
        }
        contract_file = project / "01_page_contracts" / f"page_{page:03d}.json"
        _write_json(contract_file, contract)
        page_dir = project / "03_evidence" / f"page_{page:03d}"
        selected = {
            "schema_version": "1.0", "page_number": page, "selected_chunks": [],
        }
        fact_plan = {
            "schema_version": "1.0", "page_number": page, "word_claims": [],
            "attachment_supplements": [], "mandatory_anchors": [], "conflicts": [],
            "provenance_ledger": [], "forbidden_new_conclusions": True,
        }
        route = {
            "schema_version": "1.0", "page_number": page, "route": "image",
            "reason": "test fixture", "features": {},
        }
        coverage = build_coverage_contract(contract, fact_plan)
        artifact_values = {
            "selected_evidence": selected,
            "fact_plan": fact_plan,
            "route": route,
            "coverage_contract": coverage,
        }
        artifact_paths = {}
        for name, value in artifact_values.items():
            artifact_path = page_dir / f"{name}.json"
            _write_json(artifact_path, value)
            artifact_paths[name] = artifact_path
        jobs.append({
            "slide_id": f"slide_{page:03d}",
            "page_number": page,
            "status": "queued" if confirmed else "pending_style_confirmation",
            **({"complexity_weight": 1} if confirmed else {}),
            "contract_file": f"01_page_contracts/page_{page:03d}.json",
            "expected_output": f"06_images/generated/page_{page:03d}.png",
            "selected_evidence_file": artifact_paths["selected_evidence"].relative_to(project).as_posix(),
            "selected_evidence_sha256": hashlib.sha256(artifact_paths["selected_evidence"].read_bytes()).hexdigest(),
            "fact_plan_file": artifact_paths["fact_plan"].relative_to(project).as_posix(),
            "fact_plan_sha256": hashlib.sha256(artifact_paths["fact_plan"].read_bytes()).hexdigest(),
            "route_file": artifact_paths["route"].relative_to(project).as_posix(),
            "route_sha256": hashlib.sha256(artifact_paths["route"].read_bytes()).hexdigest(),
            "route": "image",
            "coverage_contract_file": artifact_paths["coverage_contract"].relative_to(project).as_posix(),
            "coverage_sha256": coverage["sha256"],
        })

    gate: dict = {"status": "pending", "confirmed_at": None}
    if confirmed:
        execution = {
            "schema_version": "1.0",
            "direction": "editorial",
            "canvas": "ppt169",
            "canvas_profile": {
                "aspect_ratio": "16:9",
                "slide_width_inches": 10.0,
                "slide_height_inches": 14.288 / 2.54,
                "fit": "reconstruct_to_body",
                "coordinate_space": "dynamic_source_normalized",
                "allow_crop": False,
            },
            "image_quality": "high",
            "body_image_profile": {
                "version": "body-image-profile-v1",
                "production_profile": "balanced",
                "size": "1904x896",
                "ratio": "17:8",
                "mapping": "direct_then_repair",
                "direct_aspect_tolerance": 0.01,
            },
            "generation_mode": generation_mode,
            "max_concurrency": max_concurrency,
            "automatic_repair_budget": 2,
            "fixed_frame": {"title_color": "#22577A", **fixed_frame_execution()},
            "hard_constraints": {
                "content_fidelity": "preserve_information_and_logic",
                "one_page_to_one_slide": True,
                "title_color": "#22577A",
                "palette": {"primary": "#22577A", "background": "#FFFFFF", "body_text": "#1F2937"},
                "typography": {"heading": {"cjk": "Microsoft YaHei"}, "type_scale_pt": {"page_title": 28}},
            },
            "soft_preferences": {
                "direction": 1,
                "template_selection": {"mode": "editorial"},
                "visual_style": "formal-consulting",
                "color": {"palette": "test"},
                "icons": "minimal",
                "typography": {"body": "Microsoft YaHei"},
                "image_rendering": {"rendering": "photographic"},
                "style_axes": {"formality": "high"},
                "information_density": "balanced",
                "layout_preferences": ["auto", "editorial"],
                "regional_style": {"region": "global"},
                "background_system": "light",
                "image_role": {"proportion": "medium-low"},
                "evidence_strength": "business",
                "composition_tendency": "formal-consulting",
                "brand_device": "light",
                "additional_requirements": "",
            },
            "creative_freedom": {
                "layout": True,
                "composition": True,
                "visual_hierarchy": True,
                "content_visualization": True,
                "page_specific_emphasis": True,
            },
        }
        digest = hashlib.sha256(canonical_json_bytes(execution)).hexdigest()
        execution_file = project / "02_style" / "style_execution.json"
        execution_file.parent.mkdir()
        execution_file.write_bytes(canonical_json_bytes(execution))
        (execution_file.parent / "style_execution.sha256").write_text(
            digest + "\n", encoding="ascii",
        )
        gate = {
            "status": "confirmed",
            "confirmed_at": "2026-07-27T00:00:00Z",
            "execution_file": "02_style/style_execution.json",
            "execution_sha256": digest,
        }
    logo = project / "00_source" / "company_logo.svg"
    logo.parent.mkdir(exist_ok=True)
    logo.write_text('<svg xmlns="http://www.w3.org/2000/svg" width="120" height="48"/>', encoding="utf-8")
    source = project / "00_source" / "source.docx"
    source.write_bytes(b"independent page fixture Word source")
    source_sha256 = hashlib.sha256(source.read_bytes()).hexdigest()
    state = {
        "schema_version": "1.0",
        "workflow_contract_version": "word-ppt-workflow-v4",
        "project_name": "independent-pages",
        "word_source": {
            "path": "00_source/source.docx",
            "sha256": source_sha256,
            "pages_path": "00_source/pages.json",
        },
        "logo_source": {
            "path": "00_source/company_logo.svg",
            "sha256": hashlib.sha256(logo.read_bytes()).hexdigest(),
            "media_type": "image/svg+xml",
        },
        "pagination": {
            "page_count": page_count,
            "locked_page_order": list(range(1, page_count + 1)),
        },
        "style_confirmation": gate,
        "jobs": jobs,
        "final_pptx": None,
    }
    if confirmed:
        state["scheduler"] = {
            "concurrency": max_concurrency,
            "configured_max": max_concurrency,
            "last_trigger": "style_confirmation",
        }
        state["runtime"] = {
            "generation_mode": generation_mode,
            "image_quality": "high",
            "automatic_repair_budget": 2,
        }
    _write_json(project / "workflow_run.json", state)
    _write_json(project / "01_page_contracts" / "source_lock.json", {
        "schema_version": "2.0",
        "workflow_contract_version": "word-ppt-workflow-v4",
        "source_file": "pages.json",
        "page_count": page_count,
        "pages": [
            {
                "page_number": job["page_number"],
                "contract_file": Path(job["contract_file"]).name,
                "contract_sha256": hashlib.sha256(
                    (project / job["contract_file"]).read_bytes()
                ).hexdigest(),
                "relationship_contract_sha256": json.loads(
                    (project / job["contract_file"]).read_text(encoding="utf-8")
                )["relationship_contract_sha256"],
            }
            for job in jobs
        ],
    })
    if confirmed:
        locked_contracts = [
            json.loads((project / job["contract_file"]).read_text(encoding="utf-8"))
            for job in jobs
        ]
        build_page_requirement_summary(project, locked_contracts)
        for job in jobs:
            contract_path = project / job["contract_file"]
            contract = json.loads(contract_path.read_text(encoding="utf-8"))
            authority = build_effective_page_authority(
                page_contract=contract,
                style_execution=execution,
                directives=[],
                page_images=[],
                attachment_evidence=[],
                search_evidence=[],
            )
            _resolved, resolution_artifact = load_verified_page_resolutions(project, contract)
            bundle = {
                "artifact_version": "page-material-bundle-v4",
                "workflow_contract_version": "word-ppt-workflow-v4",
                "page_number": job["page_number"],
                "source_text": contract["source_text"],
                "source_hash": contract["source_hash"],
                "authoritative_content": {"body_text": contract["body_text"], "tables": []},
                "style_execution": {"path": "02_style/style_execution.json", "sha256": gate["execution_sha256"]},
                "page_images": [],
                "required_presence_asset_ids": [],
                "comment_intents": [],
                "resolved_directives": [],
                "effective_page_authority": authority,
                "required_directives": [],
                "superseded_directives": [],
                "generation_readiness": {"ready": True, "code": "ready", "directive_ids": [], "blocking_reasons": []},
                "attachment_evidence": [],
                "search_evidence": [],
                "material_summary": _material_summary([], [], []),
                "provenance": {
                    "project_id": "independent-pages",
                    "source_sha256": source_sha256,
                    "page_contract_sha256": hashlib.sha256(contract_path.read_bytes()).hexdigest(),
                    "logo_sha256": state["logo_source"]["sha256"],
                    "raw_page_comments": [],
                    "resolution_receipts": [],
                    "comment_resolution_artifact": resolution_artifact,
                },
            }
            bundle["bundle_attestation_signature"] = sign_project_payload(
                project,
                _bundle_attestation_payload(bundle),
                purpose=_BUNDLE_ATTESTATION_PURPOSE,
            )
            bundle["sealed_sha256"] = _seal_digest(bundle)
            bundle_path = project / "04_v4" / "material" / f"page_{job['page_number']:03d}.json"
            bundle_path.parent.mkdir(parents=True, exist_ok=True)
            bundle_path.write_bytes(canonical_json_bytes(bundle))
            job["material_bundle_file"] = bundle_path.relative_to(project).as_posix()
            job["material_bundle_sha256"] = bundle["sealed_sha256"]
            job["material_bundle_file_sha256"] = hashlib.sha256(bundle_path.read_bytes()).hexdigest()
            job["material_authority_input_identity"] = (
                workflow_state._material_authority_input_identity(project, state, job)
            )
        _write_json(project / "workflow_run.json", state)
    return project


def _set_material_readiness(
    project: Path, page: int, *, ready: bool, material_kind: str = "search",
) -> None:
    job = _job(project, page)
    path = project / job["material_bundle_file"]
    bundle = json.loads(path.read_text(encoding="utf-8"))
    contract = json.loads((project / job["contract_file"]).read_text(encoding="utf-8"))
    style = json.loads((project / "02_style/style_execution.json").read_text(encoding="utf-8"))
    target_by_kind = {
        "search": "material.search_evidence",
        "page_image": "material.page_image",
        "attachment": "material.attachment",
    }
    code_by_kind = {
        "search": "required_search_material_unavailable",
        "page_image": "required_page_image_unavailable",
        "attachment": "required_attachment_unavailable",
    }
    target = target_by_kind[material_kind]
    material_id = "search-request-0123456789abcdef" if material_kind == "search" else f"missing-{material_kind}"
    comment = {
        "comment_id": "fixture-material-comment",
        "text": "需要外部新闻图片",
        "author": "fixture",
        "timestamp": None,
    }
    contract["page_comments"] = [comment]
    contract_path = project / job["contract_file"]
    _write_json(contract_path, contract)
    lock_path = project / "01_page_contracts/source_lock.json"
    lock = json.loads(lock_path.read_text(encoding="utf-8"))
    lock_record = next(item for item in lock["pages"] if item["page_number"] == page)
    lock_record["contract_sha256"] = hashlib.sha256(contract_path.read_bytes()).hexdigest()
    _write_json(lock_path, lock)
    directives = [{
        "directive_id": "comment-search-1",
        "kind": "note" if ready else "material_requirement",
        "text": "需要外部新闻图片",
        "decisions": [] if ready else [{
            "target": target,
            "action": "require",
            "material_id": material_id,
        }],
    }]
    authority = build_effective_page_authority(
        page_contract=contract,
        style_execution=style,
        directives=directives,
        page_images=[],
        attachment_evidence=[],
        search_evidence=[],
    )
    reasons = [] if ready else [{
        "code": code_by_kind[material_kind],
        "directive_id": "comment-search-1",
        "target": target,
        "material_id": material_id,
    }]
    bundle["resolved_directives"] = directives
    bundle["effective_page_authority"] = authority
    bundle["required_directives"] = authority["required_directives"]
    bundle["generation_readiness"] = {
        "ready": ready,
        "code": "ready" if ready else code_by_kind[material_kind],
        "directive_ids": [] if ready else ["comment-search-1"],
        "blocking_reasons": reasons,
    }
    closed_digest = hashlib.sha256(
        json.dumps(directives[0], ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    bundle["provenance"]["page_contract_sha256"] = hashlib.sha256(contract_path.read_bytes()).hexdigest()
    bundle["provenance"]["raw_page_comments"] = [comment]
    bundle["provenance"]["resolution_receipts"] = [{
        "receipt_version": "comment-resolution-receipt-v1",
        "source_comment_id": comment["comment_id"],
        "raw_comment_sha256": hashlib.sha256(comment["text"].encode("utf-8")).hexdigest(),
        "directive_id": "comment-search-1",
        "closed_directive_sha256": closed_digest,
        "resolution_mode": "deterministic",
        "role": "comment-resolution",
    }]
    resolution_path = project / "confirm_ui/page_requirement_summary.json"
    resolution = json.loads(resolution_path.read_text(encoding="utf-8"))
    resolution["projectAuthority"]["sourceLockSha256"] = hashlib.sha256(lock_path.read_bytes()).hexdigest()
    page_resolution = resolution["pages"][page - 1]
    page_resolution["pageContractSha256"] = hashlib.sha256(contract_path.read_bytes()).hexdigest()
    page_resolution["pageLockSha256"] = hashlib.sha256(
        json.dumps(lock_record, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    page_resolution["rawCommentsSha256"] = hashlib.sha256(
        json.dumps([comment], ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    page_resolution["directives"] = ["需要外部新闻图片"]
    requires_search = not ready and material_kind == "search"
    page_resolution["plannedSearches"] = ["新闻图片"] if requires_search else []
    action_by_kind = {
        "search": "搜索并提供外部图片素材",
        "page_image": "使用本页Word图片素材",
        "attachment": "使用附件素材",
    }
    page_resolution["materialActions"] = [] if ready else [action_by_kind[material_kind]]
    page_resolution["rejectedHardRuleOverrides"] = []
    page_resolution["closedDirectives"] = [{
        "directive": directives[0],
        "source_comment_id": comment["comment_id"],
        "semantic_kind": "advisory" if ready else "external_image",
        "required": not ready,
        "visual_overrides": {},
        "search_required": requires_search,
        "search_query": "新闻图片" if requires_search else None,
        "resolution_receipt": bundle["provenance"]["resolution_receipts"][0],
    }]
    page_resolution["pageEntrySha256"] = _seal_page_entry(page_resolution)
    page_resolution["pageEntrySignature"] = _sign_page_entry(project, page_resolution)
    resolution["sealed_sha256"] = _resolution_seal(resolution)
    resolution["projectSignature"] = sign_project_payload(
        project,
        _resolution_signature_payload(resolution),
        purpose="page-comment-resolution-v1",
    )
    _write_json(resolution_path, resolution)
    _resolved, resolution_artifact = load_verified_page_resolutions(project, contract)
    bundle["provenance"]["comment_resolution_artifact"] = resolution_artifact
    bundle["bundle_attestation_signature"] = sign_project_payload(
        project,
        _bundle_attestation_payload(bundle),
        purpose=_BUNDLE_ATTESTATION_PURPOSE,
    )
    bundle["sealed_sha256"] = _seal_digest(bundle)
    _write_json(path, bundle)
    state_path = project / "workflow_run.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    current = next(item for item in state["jobs"] if item["page_number"] == page)
    current["material_bundle_sha256"] = bundle["sealed_sha256"]
    current["material_bundle_file_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    current["material_authority_input_identity"] = (
        workflow_state._material_authority_input_identity(project, state, current)
    )
    _write_json(state_path, state)


def _reseal_bundle_materials(project: Path, bundle: dict) -> None:
    authority = bundle["effective_page_authority"]
    authority["evidence_material"] = {
        "page_images": copy.deepcopy(bundle["page_images"]),
        "attachment_evidence": copy.deepcopy(bundle["attachment_evidence"]),
        "search_evidence": copy.deepcopy(bundle["search_evidence"]),
    }
    authority["sealed_sha256"] = _authority_seal_digest(authority)
    bundle["material_summary"] = _material_summary(
        bundle["page_images"], bundle["attachment_evidence"], bundle["search_evidence"]
    )
    bundle["bundle_attestation_signature"] = sign_project_payload(
        project, _bundle_attestation_payload(bundle), purpose=_BUNDLE_ATTESTATION_PURPOSE,
    )
    bundle["sealed_sha256"] = _seal_digest(bundle)


def _add_authenticated_text_attachment(
    project: Path, page: int, name: str, *, under_source: bool = True,
) -> tuple[Path, dict]:
    job = _job(project, page)
    material_path = project / job["material_bundle_file"]
    bundle = json.loads(material_path.read_text(encoding="utf-8"))
    root = "00_source/word_assets/derived" if under_source else f"03_evidence/page_{page:03d}"
    attachment = project / root / name
    attachment.parent.mkdir(parents=True, exist_ok=True)
    attachment.write_text(f"authenticated attachment for page {page}", encoding="utf-8")
    asset_id = f"page-{page}-attachment"
    bundle["attachment_evidence"] = [{
        "evidence_id": f"attachment-{page}",
        "asset_id": asset_id,
        "path": attachment.relative_to(project).as_posix(),
        "sha256": hashlib.sha256(attachment.read_bytes()).hexdigest(),
        "media_type": "text/plain",
        **_decode_text_evidence(attachment, asset_id=asset_id),
    }]
    _reseal_bundle_materials(project, bundle)
    _write_json(material_path, bundle)
    run = load(project)
    current = next(item for item in run["jobs"] if item["page_number"] == page)
    current["material_bundle_sha256"] = bundle["sealed_sha256"]
    current["material_bundle_file_sha256"] = hashlib.sha256(material_path.read_bytes()).hexdigest()
    _write_json(project / "workflow_run.json", run)
    return attachment, bundle


def _closure_material_kwargs(project: Path, page: int) -> dict:
    job = _job(project, page)
    bundle = json.loads((project / job["material_bundle_file"]).read_text(encoding="utf-8"))
    return {
        "material_bundle_path": project / job["material_bundle_file"],
        "material_bundle_file_sha256": job["material_bundle_file_sha256"],
        "material_bundle_sha256": job["material_bundle_sha256"],
        "authority_identity": bundle["effective_page_authority"]["sealed_sha256"],
    }


def test_reconstruction_closure_uses_only_current_authenticated_material_paths(
    tmp_path: Path,
) -> None:
    project = _project(tmp_path, page_count=2)
    attachment, _bundle = _add_authenticated_text_attachment(project, 2, "page2.txt")
    unrelated = project / "00_source/word_assets/derived/page1-only.txt"
    unrelated.write_text("page one only", encoding="utf-8")
    forged_seed = project / "07_editable/page_002/forged.json"
    _write_json(forged_seed, {"page_images": [{"path": unrelated.relative_to(project).as_posix()}]})

    closure = collect_reconstruction_closure(
        project,
        seeds=[project / _job(project, 2)["material_bundle_file"], forged_seed],
        page_number=2,
        **_closure_material_kwargs(project, 2),
    )

    assert attachment.relative_to(project).as_posix() in closure
    assert forged_seed.relative_to(project).as_posix() in closure
    assert unrelated.relative_to(project).as_posix() not in closure


def test_reconstruction_closure_includes_authenticated_material_outside_source_tree(
    tmp_path: Path,
) -> None:
    project = _project(tmp_path, page_count=1)
    attachment, _bundle = _add_authenticated_text_attachment(
        project, 1, "page1-evidence.txt", under_source=False,
    )

    closure = collect_reconstruction_closure(
        project,
        seeds=[project / _job(project, 1)["material_bundle_file"]],
        page_number=1,
        **_closure_material_kwargs(project, 1),
    )

    assert attachment.relative_to(project).as_posix() in closure


def test_reconstruction_closure_rejects_cross_page_material_identity(tmp_path: Path) -> None:
    project = _project(tmp_path, page_count=2)
    page1 = _closure_material_kwargs(project, 1)

    with pytest.raises(ValueError, match="authenticated current page material bundle"):
        collect_reconstruction_closure(
            project,
            seeds=[page1["material_bundle_path"]],
            page_number=2,
            **page1,
        )


def test_reconstruction_closure_rejects_rehmaced_live_bundle_outside_expected_identity(
    tmp_path: Path,
) -> None:
    project = _project(tmp_path, page_count=2)
    expected = _closure_material_kwargs(project, 2)
    material_path = expected["material_bundle_path"]
    bundle = json.loads(material_path.read_text(encoding="utf-8"))
    page1_only = project / "00_source/word_assets/derived/page1-only.txt"
    page1_only.parent.mkdir(parents=True, exist_ok=True)
    page1_only.write_text("page one only", encoding="utf-8")
    asset_id = "forged-page1-attachment"
    bundle["attachment_evidence"] = [{
        "evidence_id": "forged-page1",
        "asset_id": asset_id,
        "path": page1_only.relative_to(project).as_posix(),
        "sha256": hashlib.sha256(page1_only.read_bytes()).hexdigest(),
        "media_type": "text/plain",
        **_decode_text_evidence(page1_only, asset_id=asset_id),
    }]
    _reseal_bundle_materials(project, bundle)
    _write_json(material_path, bundle)

    with pytest.raises(ValueError, match="authenticated current page material bundle"):
        collect_reconstruction_closure(
            project,
            seeds=[material_path],
            page_number=2,
            **expected,
        )


def test_completed_cache_extra_source_cannot_self_authorize_from_manifest(tmp_path: Path) -> None:
    project = _project(tmp_path, page_count=1)
    unrelated = project / "00_source/word_assets/derived/extra.txt"
    cached = project / ".cache/pages/forged/extra.txt"
    unrelated.parent.mkdir(parents=True, exist_ok=True)
    cached.parent.mkdir(parents=True, exist_ok=True)
    unrelated.write_text("extra", encoding="utf-8")
    cached.write_bytes(unrelated.read_bytes())
    hit = SimpleNamespace(
        path=project / ".cache/pages/forged",
        manifest={
            "artifact_version": "v4-editable-page-cache-v3",
            "logical_files": {
                unrelated.relative_to(project).as_posix(): {
                    "path": "extra.txt",
                    "sha256": hashlib.sha256(cached.read_bytes()).hexdigest(),
                },
            },
        },
    )
    bundle = json.loads(
        (project / _job(project, 1)["material_bundle_file"]).read_text(encoding="utf-8")
    )

    with pytest.raises(ValueError, match="non-page-local global source|completed semantic inventory"):
        restore_and_validate_completed_cache(
            project,
            _job(project, 1),
            hit,
            authority_identity=bundle["effective_page_authority"]["sealed_sha256"],
        )


@pytest.mark.parametrize(
    "logical",
    ["00_source\\source.docx", ".\\00_source\\source.docx", "00_SOURCE/source.docx"],
)
def test_completed_cache_rejects_noncanonical_global_source_aliases_before_restore(
    tmp_path: Path, logical: str,
) -> None:
    project = _project(tmp_path, page_count=1)
    source = project / "00_source/source.docx"
    cached = project / ".cache/pages/forged/source.docx"
    cached.parent.mkdir(parents=True, exist_ok=True)
    cached.write_bytes(source.read_bytes())
    hit = SimpleNamespace(
        path=project / ".cache/pages/forged",
        manifest={
            "artifact_version": "v4-editable-page-cache-v3",
            "logical_files": {
                logical: {
                    "path": "source.docx",
                    "sha256": hashlib.sha256(cached.read_bytes()).hexdigest(),
                },
            },
        },
    )
    bundle = json.loads(
        (project / _job(project, 1)["material_bundle_file"]).read_text(encoding="utf-8")
    )

    with pytest.raises(ValueError, match="non-page-local global source|completed semantic inventory"):
        restore_and_validate_completed_cache(
            project,
            _job(project, 1),
            hit,
            authority_identity=bundle["effective_page_authority"]["sealed_sha256"],
        )


def test_completed_cache_payload_path_cannot_escape_cache_hit_directory(tmp_path: Path) -> None:
    project = _project(tmp_path, page_count=1)
    hit_path = project / ".cache/pages/forged"
    hit_path.mkdir(parents=True, exist_ok=True)
    logo = project / "00_source/company_logo.svg"
    hit = SimpleNamespace(
        path=hit_path,
        manifest={
            "artifact_version": "v4-editable-page-cache-v3",
            "logical_files": {
                "00_source/company_logo.svg": {
                    "path": "../../../00_source/company_logo.svg",
                    "sha256": hashlib.sha256(logo.read_bytes()).hexdigest(),
                },
            },
        },
    )
    bundle = json.loads(
        (project / _job(project, 1)["material_bundle_file"]).read_text(encoding="utf-8")
    )

    with pytest.raises(ValueError, match="cache payload path|completed semantic inventory"):
        restore_and_validate_completed_cache(
            project,
            _job(project, 1),
            hit,
            authority_identity=bundle["effective_page_authority"]["sealed_sha256"],
        )


def _thaw_cache_value(value):
    if isinstance(value, Mapping):
        return {key: _thaw_cache_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_thaw_cache_value(item) for item in value]
    return value


def _real_completed_cache(tmp_path: Path, *, page_count: int = 2, page: int = 2):
    project = _project(tmp_path, page_count=page_count)
    next_action(project)
    attempt = _dispatch_generation(project, page, f"page-{page}-worker")
    _record_image(project, page, f"page-{page}-worker", attempt)
    record_qa(
        project, page, f"page-{page}-worker", attempt,
        PageQAResult("pass", "none"),
    )
    _complete_real_reconstruction(project, page, attempt)
    job = _job(project, page)
    hit = CacheStore(project).lookup("pages", job["cache"]["key"])
    assert hit is not None
    clone = SimpleNamespace(path=hit.path, manifest=_thaw_cache_value(hit.manifest))
    bundle = json.loads((project / job["material_bundle_file"]).read_text(encoding="utf-8"))
    return project, job, clone, bundle["effective_page_authority"]["sealed_sha256"]


def _snapshot_outside_cache(project: Path) -> tuple[tuple[str, ...], dict[str, bytes]]:
    paths = []
    files = {}
    for path in sorted(project.parent.rglob("*")):
        try:
            relative_project = path.relative_to(project)
        except ValueError:
            relative_project = None
        if relative_project is not None and relative_project.parts[:1] == (".workflow_cache",):
            continue
        relative = path.relative_to(project.parent).as_posix()
        paths.append(relative + ("/" if path.is_dir() else ""))
        if path.is_file():
            files[relative] = path.read_bytes()
    return tuple(paths), files


def _add_manifest_logical(hit, logical: str, payload: bytes) -> None:
    name = hashlib.sha256(logical.encode("utf-8")).hexdigest()[:16] + ".bin"
    cached = hit.path / "adversarial" / name
    cached.parent.mkdir(parents=True, exist_ok=True)
    cached.write_bytes(payload)
    hit.manifest["logical_files"][logical] = {
        "path": cached.relative_to(hit.path).as_posix(),
        "sha256": hashlib.sha256(payload).hexdigest(),
    }


@pytest.mark.parametrize("case", ["junk", "cross_page", "known_role_extra"])
def test_completed_cache_rejects_manifest_extra_logicals_without_project_mutation(
    tmp_path: Path, case: str,
) -> None:
    project, job, hit, authority = _real_completed_cache(tmp_path)
    if case == "junk":
        logical, payload = "junk/extra.txt", b"junk"
    elif case == "cross_page":
        logical = "01_page_contracts/page_001.json"
        payload = (project / logical).read_bytes()
    else:
        logical, payload = "03_evidence/page_002/unregistered-evidence.txt", b"extra"
    _add_manifest_logical(hit, logical, payload)
    before = _snapshot_outside_cache(project)

    with pytest.raises(ValueError, match="exact completed-page inventory"):
        restore_and_validate_completed_cache(
            project, job, hit, authority_identity=authority,
        )

    assert _snapshot_outside_cache(project) == before


def test_completed_cache_rejects_missing_expected_logical_without_project_mutation(
    tmp_path: Path,
) -> None:
    project, job, hit, authority = _real_completed_cache(tmp_path)
    hit.manifest["logical_files"].pop("01_page_contracts/page_002.json")
    before = _snapshot_outside_cache(project)

    with pytest.raises(ValueError, match="exact completed-page inventory"):
        restore_and_validate_completed_cache(
            project, job, hit, authority_identity=authority,
        )

    assert _snapshot_outside_cache(project) == before


@pytest.mark.parametrize(
    ("original", "alias"),
    [
        ("00_source/company_logo.svg", "00_SOURCE/company_logo.svg"),
        (
            "@schema/editable_receipt_v4.schema.json",
            "@SCHEMA/editable_receipt_v4.schema.json",
        ),
    ],
)
def test_completed_cache_rejects_single_case_alias_without_project_mutation(
    tmp_path: Path, original: str, alias: str,
) -> None:
    project, job, hit, authority = _real_completed_cache(tmp_path)
    hit.manifest["logical_files"][alias] = hit.manifest["logical_files"].pop(original)
    before = _snapshot_outside_cache(project)

    with pytest.raises(ValueError, match="exact completed-page inventory"):
        restore_and_validate_completed_cache(
            project, job, hit, authority_identity=authority,
        )

    assert _snapshot_outside_cache(project) == before


def test_completed_cache_semantic_failure_rolls_back_every_restored_destination(
    tmp_path: Path,
) -> None:
    project, job, hit, authority = _real_completed_cache(tmp_path)
    destination = project / job["page_package"]
    destination.unlink()
    logical = job["page_package"]
    logical_record = hit.manifest["logical_files"][logical]
    cached_logical = hit.path / logical_record["path"]
    cached_logical.write_bytes(b"{}\n")
    logical_record["sha256"] = hashlib.sha256(b"{}\n").hexdigest()
    before = _snapshot_outside_cache(project)

    with pytest.raises(
        (ValueError, OSError), match="reconstruction|PPTX|package|artifact|editable page",
    ):
        restore_and_validate_completed_cache(
            project, job, hit, authority_identity=authority,
        )

    assert _snapshot_outside_cache(project) == before


def test_completed_cache_exact_inventory_restores_valid_missing_output(tmp_path: Path) -> None:
    project, job, hit, authority = _real_completed_cache(tmp_path)
    destination = project / job["editable_page"]["path"]
    expected = (hit.path / "reconstruction/page.pptx").read_bytes()
    preserved = {
        "00_source/company_logo.svg",
        job["contract_file"],
        job["material_bundle_file"],
        "02_style/style_execution.json",
    }
    removed = []
    for logical in hit.manifest["logical_files"]:
        if logical.startswith("@schema/") or logical in preserved:
            continue
        path = project / logical
        if path.is_file():
            path.unlink()
            removed.append(logical)

    restore_and_validate_completed_cache(
        project, job, hit, authority_identity=authority,
    )

    assert destination.read_bytes() == expected
    assert removed
    assert all((project / logical).is_file() for logical in removed)


def test_completed_cache_rejects_all_path_alias_forms_without_project_mutation(
    tmp_path: Path,
) -> None:
    project, job, baseline, authority = _real_completed_cache(tmp_path)
    logo_record = baseline.manifest["logical_files"]["00_source/company_logo.svg"]
    aliases = [
        "00_SOURCE/company_logo.svg",
        "00_source\\company_logo.svg",
        "./00_source/company_logo.svg",
        "00_source/./company_logo.svg",
        "../company_logo.svg",
        "C:/project/company_logo.svg",
        "//server/share/company_logo.svg",
        "00_source／company_logo.svg",
    ]
    for alias in aliases:
        hit = SimpleNamespace(
            path=baseline.path,
            manifest=_thaw_cache_value(baseline.manifest),
        )
        hit.manifest["logical_files"][alias] = dict(logo_record)
        before = _snapshot_outside_cache(project)
        with pytest.raises(ValueError, match="exact completed-page inventory|non-canonical"):
            restore_and_validate_completed_cache(
                project, job, hit, authority_identity=authority,
            )
        assert _snapshot_outside_cache(project) == before


def test_completed_cache_rejects_missing_signed_qa_role_without_project_mutation(
    tmp_path: Path,
) -> None:
    project, job, hit, authority = _real_completed_cache(tmp_path)
    logical = next(
        key for key in hit.manifest["logical_files"]
        if key.endswith(".observation.json")
    )
    hit.manifest["logical_files"].pop(logical)
    before = _snapshot_outside_cache(project)

    with pytest.raises(ValueError, match="exact completed-page inventory"):
        restore_and_validate_completed_cache(
            project, job, hit, authority_identity=authority,
        )

    assert _snapshot_outside_cache(project) == before


def test_completed_cache_expected_role_payload_cannot_escape_cache_root(
    tmp_path: Path,
) -> None:
    project, job, hit, authority = _real_completed_cache(tmp_path)
    logical = job["material_bundle_file"]
    hit.manifest["logical_files"][logical]["path"] = "../manifest.json"
    before = _snapshot_outside_cache(project)

    with pytest.raises(ValueError, match="payload path"):
        restore_and_validate_completed_cache(
            project, job, hit, authority_identity=authority,
        )

    assert _snapshot_outside_cache(project) == before


def test_completed_cache_exclusive_create_race_never_deletes_competitor_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, job, hit, authority = _real_completed_cache(tmp_path)
    destination = project / job["page_package"]
    destination.unlink()
    competitor = b"competitor-owned-by-another-writer"
    original_open = Path.open
    injected = False

    def racing_open(path: Path, mode="r", *args, **kwargs):
        nonlocal injected
        if path == destination and mode == "xb" and not injected:
            injected = True
            with original_open(path, "wb") as handle:
                handle.write(competitor)
        return original_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", racing_open)

    with pytest.raises(FileExistsError):
        restore_and_validate_completed_cache(
            project, job, hit, authority_identity=authority,
        )

    assert injected is True
    assert destination.read_bytes() == competitor


def test_completed_cache_copy_interruption_removes_only_owned_partial_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, job, hit, authority = _real_completed_cache(tmp_path)
    destination = project / job["page_package"]
    destination.unlink()
    before = _snapshot_outside_cache(project)

    def interrupted_copy(input_handle, output_handle, *, length):
        output_handle.write(b"owned-partial")
        output_handle.flush()
        raise KeyboardInterrupt

    monkeypatch.setattr(v4_reconstruction.shutil, "copyfileobj", interrupted_copy)

    with pytest.raises(KeyboardInterrupt):
        restore_and_validate_completed_cache(
            project, job, hit, authority_identity=authority,
        )

    assert destination.exists() is False
    assert _snapshot_outside_cache(project) == before


def test_completed_cache_rollback_preserves_post_publish_competitor_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, job, hit, authority = _real_completed_cache(tmp_path)
    destination = project / job["page_package"]
    destination.unlink()
    competitor = b"competitor-replaced-after-publish"

    def replace_then_fail(*args, **kwargs):
        destination.unlink()
        destination.write_bytes(competitor)
        raise ValueError("forced semantic failure")

    monkeypatch.setattr(
        v4_reconstruction, "_verify_completed_page_semantics", replace_then_fail,
    )

    with pytest.raises(ValueError, match="forced semantic failure"):
        restore_and_validate_completed_cache(
            project, job, hit, authority_identity=authority,
        )

    assert destination.read_bytes() == competitor


def test_completed_cache_rollback_preserves_recreated_transaction_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, job, hit, authority = _real_completed_cache(tmp_path)
    destination = project / job["page_package"]
    owned_directory = destination.parent
    shutil.rmtree(owned_directory)

    def replace_directory_then_fail(*args, **kwargs):
        shutil.rmtree(owned_directory)
        owned_directory.mkdir()
        raise ValueError("forced semantic failure")

    monkeypatch.setattr(
        v4_reconstruction,
        "_verify_completed_page_semantics",
        replace_directory_then_fail,
    )

    with pytest.raises(ValueError, match="forced semantic failure"):
        restore_and_validate_completed_cache(
            project, job, hit, authority_identity=authority,
        )

    assert owned_directory.is_dir()
    assert list(owned_directory.iterdir()) == []


@pytest.mark.parametrize(
    "overrides",
    [
        {"st_dev": 123, "st_ino": 0, "st_birthtime_ns": 456},
        {"st_dev": 0, "st_ino": 123, "st_birthtime_ns": 456},
        {"st_dev": 123, "st_ino": 456},
    ],
)
def test_filesystem_identity_rejects_uncertain_stable_components(
    tmp_path: Path, overrides: dict[str, int],
) -> None:
    path = tmp_path / "owned.bin"
    path.write_bytes(b"owned")
    current = path.lstat()
    values = {
        "st_mode": current.st_mode,
        "st_file_attributes": getattr(current, "st_file_attributes", 0),
        **overrides,
    }

    assert v4_reconstruction._filesystem_identity(
        path, SimpleNamespace(**values),
    ) is None


def test_filesystem_identity_accessor_failure_is_uncertain_not_exception(
    tmp_path: Path,
) -> None:
    path = tmp_path / "owned.bin"
    path.write_bytes(b"owned")
    current = path.lstat()

    class FailingIdentity:
        st_mode = current.st_mode
        st_file_attributes = getattr(current, "st_file_attributes", 0)
        st_ino = 123
        st_birthtime_ns = 456

        @property
        def st_dev(self):
            raise OSError("identity unavailable")

    assert v4_reconstruction._filesystem_identity(path, FailingIdentity()) is None


def test_completed_cache_rollback_preserves_file_with_uncertain_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, job, hit, authority = _real_completed_cache(tmp_path)
    destination = project / job["page_package"]
    cached_record = hit.manifest["logical_files"][job["page_package"]]
    expected = (hit.path / cached_record["path"]).read_bytes()
    destination.unlink()
    monkeypatch.setattr(v4_reconstruction, "_owned_handle_identity", lambda *args: None)
    monkeypatch.setattr(
        v4_reconstruction,
        "_verify_completed_page_semantics",
        lambda *args, **kwargs: (_ for _ in ()).throw(ValueError("forced semantic failure")),
    )

    with pytest.raises(ValueError, match="forced semantic failure"):
        restore_and_validate_completed_cache(
            project, job, hit, authority_identity=authority,
        )

    assert destination.read_bytes() == expected


def test_completed_cache_rollback_preserves_directory_with_uncertain_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, job, hit, authority = _real_completed_cache(tmp_path)
    owned_directory = (project / job["page_package"]).parent
    shutil.rmtree(owned_directory)
    original_identity = v4_reconstruction._owned_path_identity
    monkeypatch.setattr(
        v4_reconstruction,
        "_owned_path_identity",
        lambda path: None if path == owned_directory else original_identity(path),
    )
    monkeypatch.setattr(
        v4_reconstruction,
        "_verify_completed_page_semantics",
        lambda *args, **kwargs: (_ for _ in ()).throw(ValueError("forced semantic failure")),
    )

    with pytest.raises(ValueError, match="forced semantic failure"):
        restore_and_validate_completed_cache(
            project, job, hit, authority_identity=authority,
        )

    assert owned_directory.is_dir()
    assert list(owned_directory.iterdir()) == []


@pytest.mark.parametrize("mutation", ["fixed_title", "cross_page"])
def test_completed_cache_rejects_rehashed_coverage_semantic_forgery_without_mutation(
    tmp_path: Path, mutation: str,
) -> None:
    project, job, hit, authority = _real_completed_cache(tmp_path)
    logical = job["coverage_contract_file"]
    destination = project / logical
    forged = json.loads(destination.read_text(encoding="utf-8"))
    if mutation == "fixed_title":
        forged["fixed_title"] = {"text": "FORGED", "required": True}
    else:
        forged["page_number"] = 1
    forged["sha256"] = coverage_sha256(forged)
    payload = (json.dumps(forged, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    record = hit.manifest["logical_files"][logical]
    cached = hit.path / record["path"]
    cached.write_bytes(payload)
    record["sha256"] = hashlib.sha256(payload).hexdigest()
    destination.unlink()
    before = _snapshot_outside_cache(project)

    with pytest.raises(ValueError, match="coverage contract"):
        restore_and_validate_completed_cache(
            project, job, hit, authority_identity=authority,
        )

    assert _snapshot_outside_cache(project) == before


def _job(project: Path, page: int) -> dict:
    return next(item for item in load(project)["jobs"] if item["page_number"] == page)


def _request(action: dict, page: int) -> dict:
    return next(item for item in action["requests"] if item["page_number"] == page)


def test_material_blocked_page_persists_without_image2_request_or_lease(tmp_path: Path) -> None:
    """Removing the pre-dispatch gate would spend Image2 work on an impossible page."""
    project = _project(tmp_path, page_count=1)
    _set_material_readiness(project, 1, ready=False)

    action = next_action(project)
    job = _job(project, 1)

    assert action["stage"] == "page_blocked"
    assert action["requests"] == []
    assert job["status"] == "material_blocked"
    assert job["assignment"] is None
    assert job["generation_calls"] == 0
    assert job["page_failure"]["code"] == "required_search_material_unavailable"
    assert job["page_failure"]["directive_ids"] == ["comment-search-1"]
    assert job["page_failure"]["material_identity"] == job["material_bundle_sha256"]
    with pytest.raises(ValueError, match="material identity has not changed"):
        release_blocked_page(project, 1)


def test_missing_comment_resolution_persists_distinct_pending_state(tmp_path: Path) -> None:
    """An interrupted resolver is pending, not falsely classified as missing material."""
    project = _project(tmp_path, page_count=1)
    (project / "confirm_ui/page_requirement_summary.json").unlink()

    action = next_action(project)
    job = _job(project, 1)

    assert action["stage"] == "page_blocked"
    assert action["requests"] == []
    assert job["status"] == "comment_resolution_pending"
    assert job["assignment"] is None
    assert job["generation_calls"] == 0
    assert job["page_failure"]["phase"] == "comment_resolution"


def test_invalid_comment_resolution_persists_distinct_blocked_state(tmp_path: Path) -> None:
    """A present but unauthenticated resolver artifact is blocked, never reused as pending."""
    project = _project(tmp_path, page_count=1)
    summary = project / "confirm_ui/page_requirement_summary.json"
    value = json.loads(summary.read_text(encoding="utf-8"))
    value["pages"][0]["page_contract_sha256"] = "0" * 64
    _write_json(summary, value)

    action = next_action(project)
    job = _job(project, 1)

    assert action["stage"] == "page_blocked"
    assert job["status"] == "comment_resolution_blocked"
    assert job["generation_calls"] == 0


def test_interrupted_material_resolution_persists_pending_and_recovers_without_image2(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A committed search/resolution interruption resumes before Image2 without becoming failure."""
    project = _project(tmp_path, page_count=1)
    original = workflow_state.ensure_material_bundle

    def interrupted(*_args, **_kwargs):
        raise ValueError("material_resolution_pending: search evidence commit interrupted")

    monkeypatch.setattr(workflow_state, "ensure_material_bundle", interrupted)
    first = next_action(project)
    pending = _job(project, 1)
    assert first["stage"] == "page_blocked"
    assert pending["status"] == "material_resolution_pending"
    assert pending["generation_calls"] == 0
    assert pending["assignment"] is None

    monkeypatch.setattr(workflow_state, "ensure_material_bundle", original)
    resumed = resume(project)
    recovered = _job(project, 1)
    assert resumed["stage"] == "page_pipeline"
    assert [item["page_number"] for item in resumed["requests"]] == [1]
    assert recovered["status"] == "queued"
    assert recovered["generation_calls"] == 0


def test_material_blocked_page_recovers_only_after_verified_bundle_identity_changes(tmp_path: Path) -> None:
    """A changed, ready sealed bundle is the recoverable material boundary."""
    project = _project(tmp_path, page_count=1)
    _set_material_readiness(project, 1, ready=False)
    next_action(project)
    blocked_identity = _job(project, 1)["material_bundle_sha256"]

    _set_material_readiness(project, 1, ready=True)
    released = release_blocked_page(project, 1)

    job = _job(project, 1)
    assert job["material_bundle_sha256"] != blocked_identity
    assert released == {"page_number": 1, "state": "queued"}
    assert job["status"] == "queued"
    assert next_action(project)["requests"][0]["action"] == "generate"


def test_retry_search_rejects_ready_bundle_that_changes_shared_comment_resolution(
    tmp_path: Path, monkeypatch,
) -> None:
    """Explicit retry must call material preflight and audit a new ready identity."""
    project = _project(tmp_path, page_count=1)
    _set_material_readiness(project, 1, ready=False)
    next_action(project)
    blocked_identity = _job(project, 1)["material_bundle_sha256"]
    provider = object()
    calls = []

    def rebuild(project_arg, run, job, **kwargs):
        calls.append(kwargs)
        old = json.loads((project_arg / job["material_bundle_file"]).read_text(encoding="utf-8"))
        contract = json.loads((project_arg / job["contract_file"]).read_text(encoding="utf-8"))
        style = json.loads((project_arg / "02_style/style_execution.json").read_text(encoding="utf-8"))
        ready_directives = [{
            "directive_id": "comment-search-1", "kind": "note",
            "text": "需要外部新闻图片", "decisions": [],
        }]
        authority = build_effective_page_authority(
            page_contract=contract, style_execution=style, directives=ready_directives,
            page_images=[], attachment_evidence=[], search_evidence=[],
        )
        old["resolved_directives"] = ready_directives
        old["effective_page_authority"] = authority
        old["required_directives"] = []
        old["generation_readiness"] = {
            "ready": True, "code": "ready", "directive_ids": [], "blocking_reasons": [],
        }
        old["provenance"]["resolution_receipts"][0]["closed_directive_sha256"] = hashlib.sha256(
            json.dumps(ready_directives[0], ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        old["bundle_attestation_signature"] = sign_project_payload(
            project_arg, _bundle_attestation_payload(old),
            purpose=_BUNDLE_ATTESTATION_PURPOSE,
        )
        old["sealed_sha256"] = _seal_digest(old)
        path = project_arg / "04_v4/material/page_001_retry.json"
        _write_json(path, old)
        job["material_bundle_file"] = path.relative_to(project_arg).as_posix()
        job["material_bundle_sha256"] = old["sealed_sha256"]
        job["material_bundle_file_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
        return old

    monkeypatch.setattr(workflow_state, "ensure_material_bundle", rebuild)

    result = retry_search(project, 1, search_provider=provider)

    job = _job(project, 1)
    assert calls == [{"force_rebuild": True, "search_provider": provider, "comment_invoke": None}]
    assert result["state"] == "material_blocked"
    assert job["material_bundle_sha256"] == blocked_identity
    assert job["material_retry_history"][-1]["outcome"] == "material_blocked"
    assert "bundle seal mismatch" in job["material_retry_history"][-1]["error"]


def test_retry_search_failure_stays_blocked_and_records_attempt(tmp_path: Path, monkeypatch) -> None:
    project = _project(tmp_path, page_count=1)
    _set_material_readiness(project, 1, ready=False)
    next_action(project)
    blocked_identity = _job(project, 1)["material_bundle_sha256"]

    def fail_rebuild(*_args, **_kwargs):
        raise RuntimeError("gateway still unavailable")

    monkeypatch.setattr(workflow_state, "ensure_material_bundle", fail_rebuild)

    assert retry_search(project, 1)["state"] == "material_blocked"
    job = _job(project, 1)
    assert job["material_bundle_sha256"] == blocked_identity
    assert job["material_retry_history"][-1]["outcome"] == "material_blocked"
    assert "gateway still unavailable" in job["material_retry_history"][-1]["error"]


def test_material_authority_input_identity_changes_with_current_logo(tmp_path: Path) -> None:
    project = _project(tmp_path, page_count=1)
    run = load(project)
    job = run["jobs"][0]
    before = workflow_state._material_authority_input_identity(project, run, job)
    logo = project / run["logo_source"]["path"]
    logo.write_text('<svg xmlns="http://www.w3.org/2000/svg" width="121" height="48"/>', encoding="utf-8")
    run["logo_source"]["sha256"] = hashlib.sha256(logo.read_bytes()).hexdigest()

    after = workflow_state._material_authority_input_identity(project, run, job)

    assert after != before


def test_page_comment_change_invalidates_only_that_page_semantic_cache(tmp_path: Path) -> None:
    project = _project(tmp_path, page_count=2)
    next_action(project)
    before = {item["page_number"]: item["cache"]["key"] for item in load(project)["jobs"]}

    _set_material_readiness(project, 1, ready=True)
    next_action(project)
    after = {item["page_number"]: item["cache"]["key"] for item in load(project)["jobs"]}

    assert after[1] != before[1]
    assert after[2] == before[2]


def test_real_page1_comment_change_keeps_page2_authenticated_cache_reusable(tmp_path: Path) -> None:
    """A globally verified summary update must expose stable signed page-entry identities."""
    project = _project(tmp_path, page_count=2)
    next_action(project)
    page2_attempt = _dispatch_generation(project, 2, "page-2-worker")
    _record_image(project, 2, "page-2-worker", page2_attempt)
    record_qa(project, 2, "page-2-worker", page2_attempt, PageQAResult("pass", "none"))
    _complete_real_reconstruction(project, 2, page2_attempt)
    before_run = load(project)
    page2_before_job = next(item for item in before_run["jobs"] if item["page_number"] == 2)
    page2_cache_before = CacheStore(project).lookup(
        "generations", page2_before_job["generation_cache"]["key"]
    )
    assert page2_cache_before is not None
    page2_cache_bytes = {
        relative: (page2_cache_before.path / record["cache_path"]).read_bytes()
        for relative, record in page2_cache_before.manifest["logical_files"].items()
    }
    page2_completed_before = CacheStore(project).lookup("pages", page2_before_job["cache"]["key"])
    assert page2_completed_before is not None
    page2_completed_bytes = {
        relative: (page2_completed_before.path / record["path"]).read_bytes()
        for relative, record in page2_completed_before.manifest["logical_files"].items()
    }
    assert "00_source/source.docx" not in page2_completed_before.manifest["logical_files"]
    before = {
        item["page_number"]: {
            "resolution": json.loads(
                (project / item["material_bundle_file"]).read_text(encoding="utf-8")
            )["provenance"]["comment_resolution_artifact"],
            "bundle": item["material_bundle_sha256"],
            "generation": item["generation_cache"]["key"],
            "page": item["cache"]["key"],
        }
        for item in before_run["jobs"]
    }
    first_job = before_run["jobs"][0]
    contract_path = project / first_job["contract_file"]
    contract = json.loads(contract_path.read_text(encoding="utf-8"))
    contract["page_comments"] = [{
        "comment_id": "page1-visual-1", "text": "文字表达图片化",
        "author": "reviewer", "timestamp": None,
    }]
    _write_json(contract_path, contract)
    lock_path = project / "01_page_contracts/source_lock.json"
    lock = json.loads(lock_path.read_text(encoding="utf-8"))
    lock["pages"][0]["contract_sha256"] = hashlib.sha256(contract_path.read_bytes()).hexdigest()
    _write_json(lock_path, lock)
    word_path = project / before_run["word_source"]["path"]
    word_path.write_bytes(word_path.read_bytes() + b"page-1-comment-change")
    refreshed_run = load(project)
    refreshed_run["word_source"]["sha256"] = hashlib.sha256(word_path.read_bytes()).hexdigest()
    _write_json(project / "workflow_run.json", refreshed_run)
    contracts = [
        json.loads((project / item["contract_file"]).read_text(encoding="utf-8"))
        for item in before_run["jobs"]
    ]
    build_page_requirement_summary(project, contracts)

    action = resume(project)
    assert action["stage"] == "page_pipeline"
    after_run = load(project)
    after = {
        item["page_number"]: {
            "resolution": json.loads(
                (project / item["material_bundle_file"]).read_text(encoding="utf-8")
            )["provenance"]["comment_resolution_artifact"],
            "bundle": item["material_bundle_sha256"],
            "generation": item["generation_cache"]["key"],
            "page": item["cache"]["key"],
        }
        for item in after_run["jobs"]
    }
    assert after[1] != before[1]
    assert after[2] == before[2]
    assert _job(project, 1)["status"] == "queued"
    assert _job(project, 2)["status"] == "complete"
    page2_cache_after = CacheStore(project).lookup(
        "generations", _job(project, 2)["generation_cache"]["key"]
    )
    assert page2_cache_after is not None
    assert {
        relative: (page2_cache_after.path / record["cache_path"]).read_bytes()
        for relative, record in page2_cache_after.manifest["logical_files"].items()
    } == page2_cache_bytes
    page2_completed_after = CacheStore(project).lookup("pages", _job(project, 2)["cache"]["key"])
    assert page2_completed_after is not None
    assert {
        relative: (page2_completed_after.path / record["path"]).read_bytes()
        for relative, record in page2_completed_after.manifest["logical_files"].items()
    } == page2_completed_bytes


def test_global_style_change_invalidates_every_page_semantic_cache(tmp_path: Path) -> None:
    project = _project(tmp_path, page_count=2)
    next_action(project)
    before = {item["page_number"]: item["cache"]["key"] for item in load(project)["jobs"]}
    style_path = project / "02_style/style_execution.json"
    original_style_bytes = style_path.read_bytes()
    execution = json.loads(style_path.read_text(encoding="utf-8"))
    execution["soft_preferences"]["information_density"] = "high"
    versioned_bytes = canonical_json_bytes(execution)
    digest = hashlib.sha256(versioned_bytes).hexdigest()
    versioned_path = style_path.parent / "versions" / f"style_execution_{digest}.json"
    versioned_path.parent.mkdir(parents=True, exist_ok=True)
    versioned_path.write_bytes(versioned_bytes)
    run = load(project)
    run["style_confirmation"]["execution_file"] = versioned_path.relative_to(project).as_posix()
    run["style_confirmation"]["execution_sha256"] = digest
    _write_json(project / "workflow_run.json", run)

    next_action(project)
    after = {item["page_number"]: item["cache"]["key"] for item in load(project)["jobs"]}

    assert style_path.read_bytes() == original_style_bytes
    assert all(after[page] != before[page] for page in (1, 2))


def test_page_content_change_invalidates_only_that_page_semantic_cache(tmp_path: Path) -> None:
    project = _project(tmp_path, page_count=2)
    next_action(project)
    before = {
        item["page_number"]: (
            item["material_bundle_sha256"], item["generation_cache"]["key"], item["cache"]["key"]
        )
        for item in load(project)["jobs"]
    }
    run = load(project)
    contract_path = project / run["jobs"][0]["contract_file"]
    contract = json.loads(contract_path.read_text(encoding="utf-8"))
    contract["body_text"] = "第一页正文内容发生变化"
    contract["body_hash"] = hashlib.sha256(contract["body_text"].encode("utf-8")).hexdigest()
    contract["source_text"] = f"{contract['page_title']}\n{contract['body_text']}"
    contract["source_hash"] = hashlib.sha256(contract["source_text"].encode("utf-8")).hexdigest()
    contract["source_blocks"][0]["text"] = contract["source_text"]
    contract["semantic_units"][0]["text"] = contract["body_text"]
    _write_json(contract_path, contract)
    refresh_current_page_artifacts(project, 1)
    contracts = [
        json.loads((project / item["contract_file"]).read_text(encoding="utf-8"))
        for item in run["jobs"]
    ]
    build_page_requirement_summary(project, contracts)

    next_action(project)
    after = {
        item["page_number"]: (
            item["material_bundle_sha256"], item["generation_cache"]["key"], item["cache"]["key"]
        )
        for item in load(project)["jobs"]
    }

    assert after[1] != before[1]
    assert after[2] == before[2]


@pytest.mark.parametrize(
    ("material_kind", "code", "target"),
    [
        ("page_image", "required_page_image_unavailable", "material.page_image"),
        ("attachment", "required_attachment_unavailable", "material.attachment"),
    ],
)
def test_missing_required_local_material_is_audited_before_image2(
    tmp_path: Path, material_kind: str, code: str, target: str,
) -> None:
    """Losing the exact missing-material target would make recovery unauditable."""
    project = _project(tmp_path, page_count=1)
    _set_material_readiness(project, 1, ready=False, material_kind=material_kind)

    assert next_action(project)["requests"] == []
    failure = _job(project, 1)["page_failure"]
    assert failure["code"] == code
    assert failure["material_targets"] == [target]


def test_zero_image_repair_budget_blocks_unresolved_background_text(tmp_path: Path):
    project = _project(tmp_path, 1)
    run = load(project)
    run["runtime"]["automatic_repair_budget"] = 0
    _write_json(project / "workflow_run.json", run)
    request = next_action(project)["requests"][0]
    attempt = dispatch(project, 1, "worker", request["attempt"])["attempt"]
    image = project / "06_images" / "generated" / f"page_001_attempt_{attempt:03d}.png"
    receipt = write_valid_generation_receipt(project, 1, attempt, image)
    record_generation(
        project, 1, "worker", attempt, image, generation_receipt=receipt,
    )

    result = record_qa(
        project,
        1,
        "worker",
        attempt,
        PageQAResult("repair", "local", (_background_text_issue(),)),
    )

    assert result["state"] == "content_blocked"
    job = _job(project, 1)
    assert job["page_failure"]["category"] == "qa_unresolved"
    assert job["qa_result"]["status"] == "repair"
    assert "reconstruction_corrections" not in job
    assert "automatic_repairs_used" not in job


def test_visual_semantic_qa_failure_enters_targeted_repair_when_budget_remains(tmp_path: Path):
    project = _project(tmp_path, 1)
    attempt = _dispatch_generation(project, 1, "worker")
    _record_image(project, 1, "worker", attempt)

    result = record_qa(
        project,
        1,
        "worker",
        attempt,
        PageQAResult("repair", "structural", (_issue("Page structure is incomplete.", "structural"),)),
    )

    assert result["state"] == "repair"
    job = _job(project, 1)
    assert job["automatic_repairs_used"] == 1


def test_local_repair_consumes_local_repair_budget(tmp_path: Path):
    project = _project(tmp_path, 1)
    attempt = _dispatch_generation(project, 1, "worker")
    _record_image(project, 1, "worker", attempt)

    result = record_qa(
        project,
        1,
        "worker",
        attempt,
        PageQAResult("repair", "local", (_background_text_issue(),)),
    )

    assert result["state"] == "repair"
    job = _job(project, 1)
    assert job["repair_feedback"]["repair_scope"] == "local"
    assert job["automatic_repairs_used"] == 1


def test_exhausted_repair_budget_preserves_resume_identity_and_does_not_block_other_pages(tmp_path: Path):
    project = _project(tmp_path, 2)
    run = load(project)
    run["runtime"]["automatic_repair_budget"] = 1
    _write_json(project / "workflow_run.json", run)
    first_attempt = _dispatch_generation(project, 1, "worker")
    _record_image(project, 1, "worker", first_attempt)
    record_qa(
        project, 1, "worker", first_attempt,
        PageQAResult("repair", "local", (_background_text_issue(),)),
    )

    repair_attempt = _dispatch_generation(project, 1, "repair-worker")
    repaired_image = _record_image(project, 1, "repair-worker", repair_attempt)
    latest_issue = _issue("Use only the latest authoritative facts.")
    blocked = record_qa(
        project, 1, "repair-worker", repair_attempt,
        PageQAResult("repair", "local", (latest_issue,)),
    )

    assert blocked["state"] == "content_blocked"
    action = next_action(project)
    assert any(request["page_number"] == 2 for request in action["requests"])
    assert release_blocked_page(project, 1)["state"] == "repair"
    resumed = _job(project, 1)
    assert resumed["status"] == "repair"
    assert resumed["repair_input_sha256"] == hashlib.sha256(repaired_image.read_bytes()).hexdigest()

    page_two_attempt = _dispatch_generation(project, 2, "page-two-worker")
    _record_image(project, 2, "page-two-worker", page_two_attempt)
    record_qa(project, 2, "page-two-worker", page_two_attempt, PageQAResult("pass", "none"))
    request = _request(next_action(project), 1)
    assert request["generation_request"]["repair"]["issues"][0] == {
        "code": "gross_readability_or_overflow",
        "message": "Correct clipping, overlap, or illegibility inside the body canvas.",
        "target": "body_readability",
    }
    assert latest_issue["message"] not in request["generation_request"]["prompt"]
    resumed_attempt = dispatch(project, 1, "resume-worker", request["attempt"])["attempt"]
    _record_image(project, 1, "resume-worker", resumed_attempt)
    assert _job(project, 1)["status"] == "qa"


def _dispatch_generation(project: Path, page: int, agent: str) -> int:
    candidate = _request(next_action(project), page)
    claimed = dispatch(project, page, agent, candidate["attempt"])
    assert claimed["action"] == "generate"
    return claimed["attempt"]


def _record_image(project: Path, page: int, agent: str, attempt: int) -> Path:
    image = project / "06_images" / "generated" / f"page_{page:03d}_attempt_{attempt:03d}.png"
    receipt = write_valid_generation_receipt(project, page, attempt, image)
    record_generation(
        project, page, agent, attempt, image, generation_receipt=receipt,
    )
    return image


def _complete_first_page_while_second_is_generating(project: Path) -> int:
    first = next_action(project)
    first_attempt = dispatch(project, 1, "agent-1", _request(first, 1)["attempt"])["attempt"]
    second_attempt = dispatch(project, 2, "agent-2", _request(first, 2)["attempt"])["attempt"]
    _record_image(project, 1, "agent-1", first_attempt)
    record_qa(project, 1, "agent-1", first_attempt, PageQAResult("pass", "none"))
    return second_attempt


def test_logo_change_invalidates_accepted_qa_cache_but_not_generation_identity(tmp_path: Path):
    project = _project(tmp_path, 1)
    attempt = _dispatch_generation(project, 1, "generator")
    _record_image(project, 1, "generator", attempt)
    record_qa(project, 1, "generator", attempt, PageQAResult("pass", "none"))
    generation_cache_key = _job(project, 1)["generation_cache"]["key"]

    logo = project / "00_source" / "company_logo.svg"
    logo.parent.mkdir(parents=True, exist_ok=True)
    logo.write_text('<svg xmlns="http://www.w3.org/2000/svg"/>', encoding="utf-8")
    run = load(project)
    run["logo_source"] = {
        "path": "00_source/company_logo.svg",
        "sha256": hashlib.sha256(logo.read_bytes()).hexdigest(),
        "media_type": "image/svg+xml",
    }
    _write_json(project / "workflow_run.json", run)

    pending = resume(project)
    assert pending["stage"] == "page_pipeline"
    after = _job(project, 1)
    assert after["generation_cache"]["key"] == generation_cache_key
    assert after["status"] == "queued"
    assert after.get("generation_cache_hit") is not True
    assert after["generation_calls"] == 1


def test_accepted_page_does_not_block_later_uncached_generation(tmp_path: Path):
    project = _project(tmp_path, page_count=4, generation_mode="continuous", max_concurrency=2)
    _complete_first_page_while_second_is_generating(project)

    scheduled = next_action(project)

    assert scheduled["stage"] == "page_pipeline"
    assert [item["page_number"] for item in scheduled["requests"]] == [3]


def test_qa_pages_do_not_prevent_every_uncached_page_from_getting_initial_image2_work(tmp_path: Path):
    project = _project(tmp_path, page_count=4, generation_mode="continuous", max_concurrency=2)
    first = next_action(project)
    for page, agent in ((1, "agent-1"), (2, "agent-2")):
        attempt = dispatch(project, page, agent, _request(first, page)["attempt"])["attempt"]
        _record_image(project, page, agent, attempt)

    second = next_action(project)

    assert second["stage"] == "page_pipeline"
    assert sorted(item["page_number"] for item in second["requests"]) == [3, 4]


def test_split_setting_cannot_hide_later_uncached_v4_pages(tmp_path: Path):
    project = _project(tmp_path, page_count=4, generation_mode="split", max_concurrency=2)
    second_attempt = _complete_first_page_while_second_is_generating(project)

    scheduled = next_action(project)

    assert [item["page_number"] for item in scheduled["requests"]] == [3]
    assert second_attempt == 1
    assert _job(project, 2)["status"] == "generating"


def test_style_confirmation_is_the_only_gate_before_independent_page_work(tmp_path: Path) -> None:
    project = _project(tmp_path, confirmed=False)

    assert next_action(project) == {
        "stage": "await_style_confirmation",
            "workflow_contract_version": "word-ppt-workflow-v4",
    }
    assert status(project)["stage"] == "await_style_confirmation"
    assert resume(project)["stage"] == "await_style_confirmation"


def test_qa_pages_yield_to_remaining_uncached_generation_before_stopping_closed(tmp_path: Path) -> None:
    project = _project(tmp_path)
    first = next_action(project)
    assert first["capacity"] == 2
    assert [(item["page_number"], item["action"]) for item in first["requests"]] == [
        (1, "generate"),
        (2, "generate"),
    ]
    first_attempt = dispatch(project, 1, "agent-a", _request(first, 1)["attempt"])["attempt"]
    second_attempt = dispatch(project, 2, "agent-b", _request(first, 2)["attempt"])["attempt"]
    _record_image(project, 1, "agent-a", first_attempt)
    _record_image(project, 2, "agent-b", second_attempt)

    mixed = status(project)
    assert mixed["stage"] == "page_pipeline"
    assert mixed["page_states"] == {"qa": [1, 2], "queued": [3]}
    scheduled = next_action(project)
    assert scheduled["stage"] == "page_pipeline"
    assert [item["page_number"] for item in scheduled["requests"]] == [3]
    assert [item["page_number"] for item in scheduled["qa_work_items"]] == [1, 2]


def test_transitions_are_atomic_and_reject_illegal_agent_or_attempt(tmp_path: Path) -> None:
    project = _project(tmp_path, page_count=1)
    attempt = _dispatch_generation(project, 1, "agent-a")
    before = copy.deepcopy(load(project))
    image = project / "wrong-owner.png"
    image.write_bytes(b"image")

    with pytest.raises(ValueError, match="agent"):
        record_generation(
            project, 1, "agent-b", attempt, image, generation_receipt=image,
        )
    assert load(project) == before
    with pytest.raises(ValueError, match="attempt"):
        record_generation(
            project, 1, "agent-a", attempt + 1, image, generation_receipt=image,
        )
    assert load(project) == before
    with pytest.raises(ValueError, match="generating"):
        record_qa(project, 1, "agent-a", attempt, PageQAResult("pass", "none"))
    assert load(project) == before
    with pytest.raises(ValueError, match="state"):
        dispatch(project, 1, "agent-a", attempt + 1)
    assert load(project) == before
    with pytest.raises(ValueError, match="agent"):
        workflow_state.record_page_failure(
            project, 1, "agent-b", attempt, "generation", "backend_error", "worker stopped",
            retryable=True,
        )
    assert load(project) == before

    blocked = workflow_state.record_page_failure(
        project, 1, "agent-a", attempt, "generation", "backend_error", "worker stopped",
        retryable=True,
    )
    assert blocked["state"] == "technical_blocked"
    assert _job(project, 1)["assignment"] is None


def test_generation_failure_is_non_dispatchable_until_explicit_release(tmp_path: Path) -> None:
    project = _project(tmp_path, page_count=2, max_concurrency=2)
    first = next_action(project)
    attempt = _request(first, 1)["attempt"]
    dispatch(project, 1, "image-worker", attempt)

    blocked = workflow_state.record_page_failure(
        project, 1, "image-worker", attempt, "generation", "authentication", "HTTP 401 token_expired",
        retryable=False,
    )

    assert blocked["state"] == "technical_blocked"
    assert blocked["phase"] == "generation"
    assert blocked["attempt_count"] == attempt
    assert _request(next_action(project), 2)["action"] == "generate"
    state = _job(project, 1)
    assert state["page_failure"]["category"] == "authentication"
    assert state["page_failure_history"] == [state["page_failure"]]

    released = workflow_state.release_blocked_page(project, 1)
    assert released == {"page_number": 1, "state": "queued"}
    assert _request(next_action(project), 1)["action"] == "generate"


def test_blocked_page_identity_change_preserves_the_block_and_failure_history(tmp_path: Path) -> None:
    project = _project(tmp_path, page_count=1)
    attempt = _dispatch_generation(project, 1, "image-worker")
    workflow_state.record_page_failure(
        project, 1, "image-worker", attempt, "generation", "backend_error", "service unavailable",
        retryable=True,
    )
    before = copy.deepcopy(_job(project, 1))
    contract_path = project / before["contract_file"]
    contract = json.loads(contract_path.read_text(encoding="utf-8"))
    contract["body_text"] = "身份已经改变的正文"
    contract["body_hash"] = hashlib.sha256(contract["body_text"].encode("utf-8")).hexdigest()
    contract["source_text"] = f"{contract['page_title']}\n{contract['body_text']}"
    contract["source_hash"] = hashlib.sha256(contract["source_text"].encode("utf-8")).hexdigest()
    contract["semantic_units"][0]["text"] = contract["body_text"]
    _write_json(contract_path, contract)
    refresh_current_page_artifacts(project, 1)

    action = next_action(project)
    after = _job(project, 1)

    assert action["stage"] == "page_blocked"
    assert action["requests"] == []
    assert after["status"] == "technical_blocked"
    assert after["page_failure"] == before["page_failure"]
    assert after["page_failure_history"] == before["page_failure_history"]


def test_v4_editable_page_recording_rejects_before_accepted_without_state_mutation(tmp_path: Path) -> None:
    project = _project(tmp_path, page_count=1)
    before = load(project)
    with pytest.raises(ValueError, match="current accepted page"):
        _record_editable_page(
            project,
            1,
            "reconstructor",
            1,
            project / "page.pptx",
            editable_receipt=project / "editable-receipt.json",
        )
    assert load(project) == before


def test_only_one_agent_can_claim_the_same_page_attempt(tmp_path: Path) -> None:
    project = _project(tmp_path, page_count=1)
    attempt = _request(next_action(project), 1)["attempt"]

    def claim(agent: str) -> str:
        try:
            return dispatch(project, 1, agent, attempt)["agent"]
        except ValueError:
            return "rejected"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(claim, ("agent-a", "agent-b")))

    assert results.count("rejected") == 1
    winner = next(result for result in results if result != "rejected")
    assert _job(project, 1)["assignment"] == {
        "action": "generate",
        "agent": winner,
        "attempt": attempt,
    }


def test_local_repair_input_identity_survives_repaired_output_and_pass_qa(tmp_path: Path) -> None:
    project = _project(tmp_path, page_count=1)
    first_attempt = _dispatch_generation(project, 1, "agent-a")
    first_image = _record_image(project, 1, "agent-a", first_attempt)
    record_qa(
        project,
        1,
        "agent-a",
        first_attempt,
        PageQAResult("repair", "local", (_background_text_issue(),)),
    )
    failed_job = _job(project, 1)
    failed_receipt = json.loads((project / failed_job["qa_receipt"]["path"]).read_text(encoding="utf-8"))
    failed_work_path = project / failed_receipt["qa_work_item"]["path"]
    failed_observation_path = project / failed_receipt["observation"]["path"]
    failed_work_sha = hashlib.sha256(failed_work_path.read_bytes()).hexdigest()
    failed_observation_sha = hashlib.sha256(failed_observation_path.read_bytes()).hexdigest()
    repair_candidate = _request(next_action(project), 1)
    assert repair_candidate["generation_request"]["endpoint"] == "images/edits"
    assert repair_candidate["generation_request"]["reference_images"][0] == str(first_image.resolve())
    assert repair_candidate["generation_request"]["image_roles"][0] == "repair_source"
    assert "prior_image" not in repair_candidate["generation_request"]
    repair_key = repair_candidate["cache_key"]

    repair_attempt = dispatch(project, 1, "agent-b", repair_candidate["attempt"])["attempt"]
    repaired_image = _record_image(project, 1, "agent-b", repair_attempt)
    repaired_job = _job(project, 1)
    assert project / repaired_job["qa_work_item"]["path"] != failed_work_path
    assert hashlib.sha256(failed_work_path.read_bytes()).hexdigest() == failed_work_sha
    assert hashlib.sha256(failed_observation_path.read_bytes()).hexdigest() == failed_observation_sha
    record_qa(project, 1, "agent-b", repair_attempt, PageQAResult("pass", "none"))

    accepted = _job(project, 1)
    assert accepted["status"] == "accepted"
    assert accepted["generation"]["image"] == repaired_image.relative_to(project).as_posix()
    assert accepted["cache"]["key"] == repair_key
    assert accepted["generation_cache"]["identity"]["generation_parameters"]["repair"] == (
        repair_candidate["generation_request"]["repair"]
    )
    assert status(project)["page_states"] == {"accepted": [1]}
    pending = next_action(project)
    assert pending["stage"] == "reconstruction_backend_pending"
    assert pending["requests"] == []


def test_accepted_generation_seals_a_strict_project_local_cache_entry(tmp_path: Path) -> None:
    project = _project(tmp_path, page_count=1)
    generation_attempt = _dispatch_generation(project, 1, "agent-a")
    _record_image(project, 1, "agent-a", generation_attempt)
    record_qa(project, 1, "agent-a", generation_attempt, PageQAResult("pass_with_advisory", "none"))

    accepted = _job(project, 1)
    hit = CacheStore(project).lookup("generations", accepted["generation_cache"]["key"])
    assert hit is not None
    assert hit.manifest["artifact_version"] == "v4-accepted-generation-cache-v2"
    assert set(hit.manifest["outputs"]) == {
        "image", "generation_receipt", "qa_work_item", "qa_observation", "qa_receipt",
        "qa_signed_invocation", "qa_gateway_request", "qa_raw_response",
    }
    qa_receipt = project / accepted["qa_receipt"]["path"]
    qa_value = json.loads(qa_receipt.read_text(encoding="utf-8"))
    observation = project / qa_value["observation"]["path"]
    observation_value = json.loads(observation.read_text(encoding="utf-8"))
    original_evidence = {
        "image": project / accepted["generation"]["image"],
        "generation_receipt": project / accepted["generation_receipt"]["path"],
        "qa_work_item": project / qa_value["qa_work_item"]["path"],
        "qa_observation": observation,
        "qa_receipt": qa_receipt,
        "qa_signed_invocation": project / observation_value["invocation"]["signed_bundle"]["path"],
        "qa_gateway_request": project / observation_value["invocation"]["request"]["path"],
        "qa_raw_response": project / observation_value["invocation"]["raw_response"]["path"],
    }
    for output_name, original in original_evidence.items():
        logical = hit.manifest["outputs"][output_name]
        cached = hit.path / hit.manifest["logical_files"][logical]["cache_path"]
        assert cached.read_bytes() == original.read_bytes()
    assert all(not logical.startswith(".private/") for logical in hit.manifest["logical_files"])
    cache_bytes = b"".join(
        (hit.path / record["cache_path"]).read_bytes()
        for record in hit.manifest["logical_files"].values()
    )
    assert b"current-contract-test-only" not in cache_bytes
    assert (project / ".private" / "qa_gateway_attestation.key").read_bytes() not in cache_bytes
    assert resume(project)["stage"] == "reconstruction_backend_pending"
    assert status(project)["capacity"] == 0


@pytest.mark.parametrize(
    "tampered_output",
    ["qa_work_item", "qa_observation", "qa_receipt", "qa_signed_invocation", "qa_gateway_request", "qa_raw_response"],
)
def test_tampered_cached_qa_evidence_cannot_restore_acceptance(
    tmp_path: Path, tampered_output: str,
) -> None:
    project = _project(tmp_path, page_count=1)
    attempt = _dispatch_generation(project, 1, "agent-a")
    _record_image(project, 1, "agent-a", attempt)
    record_qa(project, 1, "agent-a", attempt, PageQAResult("pass", "none"))
    accepted = _job(project, 1)
    hit = CacheStore(project).lookup("generations", accepted["generation_cache"]["key"])
    assert hit is not None
    logical = hit.manifest["outputs"][tampered_output]
    evidence = hit.path / hit.manifest["logical_files"][logical]["cache_path"]
    evidence.write_bytes(evidence.read_bytes() + b" ")

    run = load(project)
    job = run["jobs"][0]
    job["status"] = "queued"
    job["generation"] = None
    job.pop("qa_result", None)
    job.pop("qa_receipt", None)
    _write_json(project / "workflow_run.json", run)

    assert resume(project)["stage"] == "page_pipeline"
    assert _job(project, 1)["status"] == "queued"


def test_self_contained_generation_cache_restores_after_original_artifacts_are_deleted(tmp_path: Path) -> None:
    project = _project(tmp_path, page_count=1)
    attempt = _dispatch_generation(project, 1, "agent-a")
    _record_image(project, 1, "agent-a", attempt)
    record_qa(project, 1, "agent-a", attempt, PageQAResult("pass", "none"))
    accepted = _job(project, 1)
    hit = CacheStore(project).lookup("generations", accepted["generation_cache"]["key"])
    assert hit is not None
    logical_paths = [
        logical for logical in hit.manifest["logical_files"]
        if not logical.startswith("__schemas/")
    ]
    for logical in logical_paths:
        original = project / logical
        if original.is_file():
            original.unlink()

    run = load(project)
    job = run["jobs"][0]
    job["status"] = "queued"
    job.pop("generation", None)
    job.pop("qa_result", None)
    job.pop("qa_receipt", None)
    job.pop("qa_work_item", None)
    _write_json(project / "workflow_run.json", run)

    assert resume(project)["stage"] == "reconstruction_backend_pending"
    restored = _job(project, 1)
    assert restored["status"] == "accepted"
    assert restored["generation_cache_hit"] is True
    assert all((project / logical).is_file() for logical in logical_paths)


def test_record_generation_does_not_promote_an_old_cache_over_an_active_lease(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _project(tmp_path, page_count=1)
    first_attempt = _dispatch_generation(project, 1, "agent-a")
    _record_image(project, 1, "agent-a", first_attempt)
    record_qa(project, 1, "agent-a", first_attempt, PageQAResult("pass", "none"))

    run = load(project)
    job = run["jobs"][0]
    next_attempt = first_attempt + 1
    job["attempt"] = next_attempt
    job["generation_calls"] += 1
    job["status"] = "generating"
    job["assignment"] = {
        "agent": "agent-b", "attempt": next_attempt, "action": "generate",
    }
    job.pop("generation", None)
    _write_json(project / "workflow_run.json", run)

    image = project / "06_images" / "generated" / f"page_001_attempt_{next_attempt:03d}.png"
    receipt = write_valid_generation_receipt(project, 1, next_attempt, image)
    monkeypatch.setattr(workflow_state, "cache_hit", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(
        workflow_state,
        "restore_and_validate_completed_cache",
        lambda *_args, **_kwargs: pytest.fail("an old page cache replaced the active lease"),
    )
    result = workflow_state.record_generation(
        project, 1, "agent-b", next_attempt, image, generation_receipt=receipt,
    )

    assert result["state"] == "qa"
    restored = _job(project, 1)
    assert restored["status"] == "qa"
    assert restored["generation"]["attempt"] == next_attempt


def test_resume_during_active_image2_lease_never_dispatches_or_overwrites_it(tmp_path: Path) -> None:
    """Crash recovery after Image2 dispatch must preserve the sole worker lease."""
    project = _project(tmp_path, page_count=1)
    request = _request(next_action(project), 1)
    dispatch(project, 1, "image2-worker", request["attempt"])
    before = copy.deepcopy(_job(project, 1))

    first = resume(project)
    second = resume(project)
    after = _job(project, 1)

    assert first["requests"] == []
    assert second["requests"] == []
    assert after["status"] == "generating"
    assert after["assignment"] == before["assignment"]
    assert after["generation_calls"] == 1
    assert after["cache"] == before["cache"]


@pytest.mark.parametrize(
    "logical_suffix",
    [
        "02_style/style_execution.json", "01_page_contracts/page_001.json",
        "04_v4/material/page_001.json", "00_source/company_logo.svg",
        "page_qa_signed_invocation_v4.schema.json",
    ],
)
def test_tampering_any_authority_or_schema_snapshot_is_a_cache_miss(
    tmp_path: Path, logical_suffix: str,
) -> None:
    project = _project(tmp_path, page_count=1)
    attempt = _dispatch_generation(project, 1, "agent-a")
    _record_image(project, 1, "agent-a", attempt)
    record_qa(project, 1, "agent-a", attempt, PageQAResult("pass", "none"))
    accepted = _job(project, 1)
    hit = CacheStore(project).lookup("generations", accepted["generation_cache"]["key"])
    assert hit is not None
    logical = next(name for name in hit.manifest["logical_files"] if name.endswith(logical_suffix))
    cached = hit.path / hit.manifest["logical_files"][logical]["cache_path"]
    cached.write_bytes(cached.read_bytes() + b" ")
    run = load(project)
    run["jobs"][0]["status"] = "queued"
    run["jobs"][0].pop("generation", None)
    _write_json(project / "workflow_run.json", run)

    assert resume(project)["stage"] == "page_pipeline"
    assert _job(project, 1)["status"] == "queued"


def test_manual_generation_cache_without_a_semantically_valid_receipt_cannot_restore_acceptance(
    tmp_path: Path,
) -> None:
    project = _project(tmp_path, page_count=1)
    next_action(project)
    job = _job(project, 1)
    cache = job["generation_cache"]
    store = CacheStore(project)
    with store.staging("generations", cache["key"]) as staged:
        image = staged / "image" / "forged.png"
        image.parent.mkdir()
        from PIL import Image

        Image.new("RGB", (34, 16), "white").save(image)
        store.seal("generations", cache["key"], staged, {
            "schema_version": 1,
            "cache_identity": cache["identity"],
            "qa_result": {"status": "pass", "repair_scope": "none", "issues": []},
            "body_image_mapping": {"mode": "direct"},
            "outputs": {"image": image.relative_to(staged).as_posix()},
            "files": [{
                "path": image.relative_to(staged).as_posix(),
                "sha256": hashlib.sha256(image.read_bytes()).hexdigest(),
            }],
        })

    logo = project / "00_source" / "company_logo.svg"
    logo.write_text('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1 1"/>', encoding="utf-8")
    run = load(project)
    run["logo_source"]["sha256"] = hashlib.sha256(logo.read_bytes()).hexdigest()
    _write_json(project / "workflow_run.json", run)

    result = resume(project)

    assert result["stage"] == "page_pipeline"
    assert _job(project, 1)["status"] == "queued"


def test_status_capacity_counts_only_ready_jobs_that_can_actually_launch(tmp_path: Path) -> None:
    project = _project(tmp_path, page_count=1)

    assert status(project)["capacity"] == 1
    assert next_action(project)["capacity"] == 1


def test_status_is_read_only_and_never_runs_full_job_reconciliation(
    tmp_path: Path, monkeypatch,
) -> None:
    project = _project(tmp_path, page_count=1)
    # Reconciliation belongs to scheduling/resume, not to subsequent status
    # polling.  Prime the fixture through that explicit state transition.
    next_action(project)
    before = (project / "workflow_run.json").read_bytes()

    monkeypatch.setattr(
        workflow_state,
        "_sync_jobs",
        lambda *_args, **_kwargs: pytest.fail("status must not reconcile every page"),
    )

    result = status(project)

    assert result["stage"] == "page_pipeline"
    assert result["page_states"] == {"queued": [1]}
    assert (project / "workflow_run.json").read_bytes() == before


def test_status_and_next_agree_when_active_weight_saturates_budget(tmp_path: Path) -> None:
    project = _project(tmp_path, page_count=11)
    for page_number in range(1, 12):
        job = _job(project, page_number)
        contract_path = project / job["contract_file"]
        contract = json.loads(contract_path.read_text(encoding="utf-8"))
        contract["source_text"] = f"第{page_number}页" + ("复杂内容" * 400)
        contract["source_hash"] = hashlib.sha256(contract["source_text"].encode("utf-8")).hexdigest()
        contract["semantic_units"] = [{"text": str(index)} for index in range(25)]
        contract["explicit_relations"] = [{"type": "sequence"} for _ in range(10)]
        contract["asset_bindings"] = [{"asset_id": "asset"}]
        _write_json(contract_path, contract)
        refresh_current_page_artifacts(project, page_number)

    resolution_path = project / "confirm_ui/page_requirement_summary.json"
    resolution_path.unlink()
    run = load(project)
    contracts = [json.loads((project / item["contract_file"]).read_text(encoding="utf-8")) for item in run["jobs"]]
    build_page_requirement_summary(project, contracts)
    for item, contract in zip(run["jobs"], contracts, strict=True):
        bundle_path = project / item["material_bundle_file"]
        bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
        _resolved, resolution_artifact = load_verified_page_resolutions(project, contract)
        bundle["provenance"]["comment_resolution_artifact"] = resolution_artifact
        bundle["bundle_attestation_signature"] = sign_project_payload(
            project, _bundle_attestation_payload(bundle), purpose=_BUNDLE_ATTESTATION_PURPOSE,
        )
        bundle["sealed_sha256"] = _seal_digest(bundle)
        _write_json(bundle_path, bundle)
        item["material_bundle_sha256"] = bundle["sealed_sha256"]
        item["material_bundle_file_sha256"] = hashlib.sha256(bundle_path.read_bytes()).hexdigest()
    _write_json(project / "workflow_run.json", run)

    first = next_action(project)
    assert first["capacity"] == 2
    dispatch(project, 1, "agent-a", _request(first, 1)["attempt"])
    second = next_action(project)
    assert second["capacity"] == 1
    dispatch(project, 2, "agent-b", _request(second, 2)["attempt"])

    assert next_action(project)["capacity"] == 0
    assert status(project)["capacity"] == 0


def test_unsealed_contract_mutation_blocks_stale_comment_resolution_artifact(tmp_path: Path) -> None:
    project = _project(tmp_path, page_count=2)
    next_action(project)
    run = load(project)
    assert all(
        item["cache"]["identity"]["page_cache_contract_version"] == "v4-editable-page-cache-v3"
        for item in run["jobs"]
    )
    store = CacheStore(project)
    for item in run["jobs"]:
        legacy_identity = dict(item["cache"]["identity"])
        legacy_identity.pop("page_cache_contract_version")
        legacy_key = hashlib.sha256(
            json.dumps(legacy_identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        with store.staging("pages", legacy_key) as staged:
            artifact = staged / "page.json"
            artifact.write_text(json.dumps({"page": item["page_number"]}), encoding="utf-8")
            store.seal("pages", legacy_key, staged, {
                "schema_version": 1,
                "cache_identity": legacy_identity,
                "files": [{"path": artifact.name, "sha256": hashlib.sha256(artifact.read_bytes()).hexdigest()}],
            })

    first_resume = resume(project)
    assert first_resume["stage"] == "page_pipeline"
    assert status(project)["cache_hits"] == []


def test_old_v2_page_cache_manifest_is_never_promoted(tmp_path: Path) -> None:
    """A semantically old page snapshot must remain a miss under cache v3."""
    project = _project(tmp_path, page_count=1)
    next_action(project)
    cache = _job(project, 1)["cache"]
    store = CacheStore(project)
    with store.staging("pages", cache["key"]) as staged:
        artifact = staged / "page.pptx"
        artifact.write_bytes(b"legacy-page")
        store.seal("pages", cache["key"], staged, {
            "artifact_version": "v4-editable-page-cache-v2",
            "schema_version": 1,
            "cache_identity": cache["identity"],
            "files": [{
                "path": artifact.name,
                "sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(),
            }],
        })

    assert workflow_state.cache_hit(project, cache) is None


@pytest.mark.parametrize(
    ("field", "legacy_version"),
    [("prompt", "page-prompt-v5"), ("qa", "risk-qa-v3")],
)
def test_completed_legacy_prompt_or_qa_identity_never_promotes(
    tmp_path: Path, field: str, legacy_version: str,
) -> None:
    """Even a correctly sealed v3 envelope cannot upgrade old semantic work."""
    project = _project(tmp_path, page_count=1)
    next_action(project)
    current = _job(project, 1)["cache"]
    identity = copy.deepcopy(current["identity"])
    if field == "prompt":
        identity["generation_parameters"]["prompt_contract_version"] = legacy_version
    else:
        identity["qa_policy_version"] = legacy_version
    key = hashlib.sha256(
        json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    cache = {"key": key, "identity": identity}
    store = CacheStore(project)
    with store.staging("pages", key) as staged:
        artifact = staged / "page.pptx"
        artifact.write_bytes(b"legacy-semantic-page")
        store.seal("pages", key, staged, {
            "artifact_version": "v4-editable-page-cache-v3",
            "schema_version": 1,
            "cache_identity": identity,
            "files": [{
                "path": artifact.name,
                "sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(),
            }],
        })

    assert workflow_state.cache_hit(project, cache) is None

    contract_path = project / _job(project, 1)["contract_file"]
    contract = json.loads(contract_path.read_text(encoding="utf-8"))
    contract["source_text"] = "第1页已经单独修改"
    contract["source_hash"] = hashlib.sha256(contract["source_text"].encode("utf-8")).hexdigest()
    _write_json(contract_path, contract)

    resumed = resume(project)
    assert resumed["stage"] == "page_blocked"
    assert _job(project, 1)["page_failure"]["code"] == "authority_changed"
    assert status(project)["cache_hits"] == []


def test_page_image_is_the_only_reference_and_its_bytes_are_in_strict_cache_identity(tmp_path: Path) -> None:
    from PIL import Image

    project = _project(tmp_path, page_count=1)
    asset = project / "00_source/word_assets/original/chart.png"
    asset.parent.mkdir(parents=True)
    Image.new("RGB", (32, 18), "navy").save(asset)
    digest = hashlib.sha256(asset.read_bytes()).hexdigest()
    run = load(project)
    job = run["jobs"][0]
    bundle_path = project / job["material_bundle_file"]
    bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
    bundle["page_images"] = [{
        "asset_id": "word_asset_001",
        "path": "00_source/word_assets/original/chart.png",
        "sha256": digest,
        "media_type": "image/png",
        "presence_policy": "reference_only",
        "promotion": None,
    }]
    _reseal_bundle_materials(project, bundle)
    _write_json(bundle_path, bundle)
    job["material_bundle_sha256"] = bundle["sealed_sha256"]
    job["material_bundle_file_sha256"] = hashlib.sha256(bundle_path.read_bytes()).hexdigest()
    _write_json(project / "workflow_run.json", run)

    first = next_action(project)["requests"][0]
    payload = first["generation_request"]
    assert payload["reference_images"] == [str(asset.resolve())]
    assert payload["image_roles"] == ["reference_only"]
    assert payload["operation"] == "edit"
    assert payload["endpoint"] == "images/edits"
    first_key = first["cache_key"]
    assert _job(project, 1)["cache"]["identity"]["page_asset_inputs"][0]["sha256"] == digest
    assert _job(project, 1)["cache"]["identity"]["generation_parameters"]["operation"] == "edit"

    Image.new("RGB", (32, 18), "red").save(asset)
    run = load(project)
    job = run["jobs"][0]
    bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
    bundle["page_images"][0]["sha256"] = hashlib.sha256(asset.read_bytes()).hexdigest()
    _reseal_bundle_materials(project, bundle)
    _write_json(bundle_path, bundle)
    job["material_bundle_sha256"] = bundle["sealed_sha256"]
    job["material_bundle_file_sha256"] = hashlib.sha256(bundle_path.read_bytes()).hexdigest()
    _write_json(project / "workflow_run.json", run)

    assert next_action(project)["requests"][0]["cache_key"] != first_key


def test_cache_identity_changes_for_each_exact_input_and_not_for_another_page() -> None:
    first_page = CacheKeyInputs(
        workflow_contract_version="word-ppt-workflow-v4",
        full_source_sha256="1" * 64,
        style_execution_sha256="2" * 64,
        page_asset_inputs=[{"asset_id": "word_asset_001", "sha256": "a" * 64, "derivation": "original_supported"}],
        generation_parameters={"model": "gpt-image-2", "quality": "high", "size": "auto"},
        repair_feedback={"repair_scope": "none", "issues": []},
        reconstruction_version="fixed-canvas-cm-v2-editable-v1",
        geometry_version="fixed-canvas-cm-v2",
        fixed_layer_version="fixed-canvas-cm-v2-native-layer-v1",
        title_sha256="3" * 64,
        logo_sha256="4" * 64,
        page_number=1,
    )
    second_page = replace(first_page, full_source_sha256="5" * 64)
    second_key = build_page_cache_key(second_page)

    variants = (
        replace(first_page, full_source_sha256="6" * 64),
        replace(first_page, style_execution_sha256="7" * 64),
        replace(first_page, page_asset_inputs=[{"asset_id": "word_asset_001", "sha256": "b" * 64, "derivation": "original_supported"}]),
        replace(first_page, generation_parameters={"model": "gpt-image-2", "quality": "medium", "size": "auto"}),
        replace(first_page, repair_feedback={"repair_scope": "local", "issues": [_issue("Fix date")]}),
        replace(first_page, reconstruction_version="fixed-canvas-cm-v2-editable-v2"),
        replace(first_page, title_sha256="8" * 64),
        replace(first_page, logo_sha256="9" * 64),
        replace(first_page, page_number=2),
    )
    assert set(first_page.payload) == {
        "workflow_contract_version",
        "full_source_sha256",
        "style_execution_sha256",
        "page_asset_inputs",
        "generation_parameters",
        "repair_feedback",
        "reconstruction_version",
        "geometry_version",
        "fixed_layer_version",
        "title_sha256",
        "logo_sha256",
        "page_number",
    }
    original_key = build_page_cache_key(first_page)
    assert all(build_page_cache_key(changed) != original_key for changed in variants)
    assert build_page_cache_key(second_page) == second_key


def test_downloaded_search_image_hash_invalidates_only_referencing_page() -> None:
    """Replacing downloaded pixels must not evict unrelated pages."""
    before_summary = _material_summary([], [], [{
        "evidence_id": "search-evidence-1", "sha256": "a" * 64,
    }])
    after_summary = _material_summary([], [], [{
        "evidence_id": "search-evidence-1", "sha256": "b" * 64,
    }])
    assert before_summary["identities_sha256"] != after_summary["identities_sha256"]
    referencing = CacheKeyInputs(
        workflow_contract_version="word-ppt-workflow-v4",
        full_source_sha256="a" * 64,
        style_execution_sha256="2" * 64,
        page_asset_inputs=[],
        generation_parameters={"model": "gpt-image-2", "quality": "high", "size": "1904x896"},
        repair_feedback={"repair_scope": "none", "issues": []},
        reconstruction_version="editable-image-v3",
        geometry_version="fixed-canvas-cm-v2",
        fixed_layer_version="native-layer-v3",
        title_sha256="3" * 64,
        logo_sha256="4" * 64,
        page_number=1,
    )
    unrelated = replace(
        referencing, page_number=2, full_source_sha256="5" * 64, page_asset_inputs=[],
    )
    referencing_after_download = replace(
        referencing, full_source_sha256="b" * 64,
    )

    unrelated_key = build_page_cache_key(unrelated)
    assert build_page_cache_key(referencing_after_download) != build_page_cache_key(referencing)
    assert build_page_cache_key(unrelated) == unrelated_key


def test_current_cli_does_not_advertise_the_removed_recovery_runtime() -> None:
    cli = ROOT / "scripts" / "word_to_editable_ppt.py"

    help_result = subprocess.run(
        [sys.executable, str(cli), "--help"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    stale_result = subprocess.run(
        [sys.executable, str(cli), "recovery", "--help"],
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert help_result.returncode == 0
    assert "recovery" not in help_result.stdout
    assert stale_result.returncode == 2
    assert "invalid choice" in stale_result.stderr


def test_production_cli_only_exposes_v6_commands() -> None:
    cli = ROOT / "scripts" / "word_to_editable_ppt.py"

    help_result = subprocess.run(
        [sys.executable, str(cli), "v6", "--help"],
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert help_result.returncode == 0
    assert "generate-page" in help_result.stdout
    assert "reconstruction-request" in help_result.stdout
    assert "finalize-page" in help_result.stdout
    assert "assemble" in help_result.stdout
    assert "record-page-failure" not in help_result.stdout
    assert "v5" not in help_result.stdout
