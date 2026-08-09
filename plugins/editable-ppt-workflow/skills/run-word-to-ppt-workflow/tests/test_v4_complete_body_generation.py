"""Task 3 contracts for the V4 complete-body Image2 cutover."""

from __future__ import annotations

import copy
import hashlib
import json
import sys
from pathlib import Path

from PIL import Image
import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import page_generation  # noqa: E402
import page_pipeline  # noqa: E402
import production_runner  # noqa: E402
import workflow_state  # noqa: E402
from body_image_profile import mapping_for_source  # noqa: E402
from contract_version import CURRENT_CONTRACT, require_supported_contract  # noqa: E402
from effective_page_authority import build_effective_page_authority  # noqa: E402
from page_material_bundle_v4 import _material_summary, _seal_digest  # noqa: E402
from prompt_compiler import compile_page_prompt  # noqa: E402
from style_contract import canonical_json_bytes  # noqa: E402
from test_page_material_bundle_v4 import _binding, _build, _contract, _project  # noqa: E402
from workflow_contract import version_vector  # noqa: E402


SHA = "a" * 64


def _style() -> tuple[dict, str]:
    execution = {
        "schema_version": "1.0",
        "canvas_profile": {
            "aspect_ratio": "16:9",
            "fit": "reconstruct_to_body",
            "coordinate_space": "dynamic_source_normalized",
            "allow_crop": False,
        },
        "body_image_profile": {
            "version": "body-image-profile-v1",
            "production_profile": "balanced",
            "size": "1904x896",
            "ratio": "17:8",
            "mapping": "direct_then_repair",
            "direct_aspect_tolerance": 0.01,
        },
        "image_quality": "high",
        "hard_constraints": {
            "content_fidelity": "preserve_information_and_logic",
            "palette": {"primary": "#22577A"},
            "typography": {"body": {"cjk": "Microsoft YaHei"}},
        },
        "soft_preferences": {
            "visual_style": "editorial",
            "image_rendering": {"rendering": "photographic"},
        },
        "creative_freedom": {"layout": True, "composition": True},
    }
    digest = hashlib.sha256(canonical_json_bytes(execution)).hexdigest()
    return execution, digest


def _sealed_bundle(style_sha256: str) -> dict:
    source_table = "| Metric | Value |\n| --- | --- |\n| Revenue | 100 |"
    source_text = (
        "Quarterly Results\nRevenue was 100 and margin was 25%.\n" + source_table
    )
    authoritative_content = {
        "body_text": "Revenue was 100 and margin was 25%.",
        "tables": [
            {
                "table_id": "table_001",
                "rows": [["Metric", "Value"], ["Revenue", "100"]],
            }
        ],
    }
    page_images = [
        {
            "asset_id": "chart_1",
            "path": "00_source/chart.png",
            "sha256": "b" * 64,
            "media_type": "image/png",
            "presence_policy": "reference_only",
            "promotion": None,
        },
        {
            "asset_id": "photo_2",
            "path": "00_source/photo.png",
            "sha256": "c" * 64,
            "media_type": "image/png",
            "presence_policy": "required_presence",
            "promotion": {
                "source": "page_comment",
                "directive_type": "require_page_image",
                "asset_id": "photo_2",
                "raw": "[require-page-image:photo_2]",
            },
        },
    ]
    search_evidence = [
        {
            "evidence_id": "search_1",
            "asset_id": "search-request-0123456789abcdef",
            "query": "revenue context",
            "source_url": "https://example.test/source",
            "excerpt": "Market demand remained stable.",
            "retrieved_at": "2026-08-01T00:00:00Z",
            "sha256": "d" * 64,
            "direct_image_url": "https://example.test/source.png",
            "final_image_url": "https://example.test/source.png",
            "title": "Verified market reference",
            "publisher": "Example",
            "local_path": "04_materials/search/source.png",
            "media_type": "image/png",
            "width": 1200,
            "height": 800,
            "material_attestation": {
                "path": "04_materials/search/source.attestation.json",
                "sha256": "2" * 64,
                "digest": "3" * 64,
                "signature": "4" * 64,
            },
        }
    ]
    resolved_directives = [
        {
            "directive_id": "directive-expression",
            "kind": "visual_override",
            "text": "文字表达图片化",
            "decisions": [
                {"target": "visual.layout", "action": "set", "value": "文字表达图片化"},
            ],
        },
        {
            "directive_id": "directive-ink",
            "kind": "visual_override",
            "text": "本页使用水墨插画",
            "decisions": [
                {"target": "visual.image_rendering", "action": "set", "value": "水墨插画"},
            ],
        },
        {
            "directive_id": "directive-photo",
            "kind": "material_requirement",
            "text": "必须使用 photo_2",
            "decisions": [
                {"target": "material.page_image", "action": "require", "material_id": "photo_2"},
            ],
        },
    ]
    execution, _ = _style()
    authority = build_effective_page_authority(
        page_contract={
            "page_number": 1,
            "body_text": authoritative_content["body_text"],
            "source_tables": [source_table],
        },
        style_execution=execution,
        directives=resolved_directives,
        page_images=page_images,
        attachment_evidence=[],
        search_evidence=search_evidence,
    )
    bundle = {
        "artifact_version": "page-material-bundle-v4",
        "workflow_contract_version": "word-ppt-workflow-v4",
        "page_number": 1,
        "source_text": source_text,
        "source_hash": hashlib.sha256(source_text.encode("utf-8")).hexdigest(),
        "authoritative_content": authoritative_content,
        "style_execution": {
            "path": "02_style/style_execution.json",
            "sha256": style_sha256,
        },
        "page_images": page_images,
        "required_presence_asset_ids": ["photo_2"],
        "comment_intents": [
            {
                "intent_id": "comment_001",
                "intent_type": "note",
                "text": "LEGACY COMMENT MUST NOT APPEAR",
                "source_comment_id": "word-comment-1",
            }
        ],
        "resolved_directives": resolved_directives,
        "effective_page_authority": authority,
        "required_directives": copy.deepcopy(authority["required_directives"]),
        "superseded_directives": copy.deepcopy(authority["superseded_directives"]),
        "generation_readiness": {
            "ready": True,
            "code": "ready",
            "directive_ids": [],
            "blocking_reasons": [],
        },
        "attachment_evidence": [],
        "search_evidence": search_evidence,
        "material_summary": _material_summary(page_images, [], search_evidence),
        "provenance": {
            "project_id": "project-1",
            "source_sha256": "e" * 64,
            "page_contract_sha256": "f" * 64,
            "logo_sha256": "1" * 64,
            "raw_page_comments": [{
                "comment_id": "word-comment-1",
                "text": "RAW COMMENT MUST NOT APPEAR",
                "author": "Reviewer",
                "timestamp": None,
            }],
            "resolution_receipts": [],
        },
        "bundle_attestation_signature": "5" * 64,
    }
    bundle["sealed_sha256"] = _seal_digest(bundle)
    return bundle


def _refresh_bundle_authority(bundle: dict, execution: dict) -> None:
    source_tables = [
        "\n".join(
            [
                "| " + " | ".join(table["rows"][0]) + " |",
                "| " + " | ".join("---" for _ in table["rows"][0]) + " |",
                *["| " + " | ".join(row) + " |" for row in table["rows"][1:]],
            ]
        )
        for table in bundle["authoritative_content"]["tables"]
    ]
    authority = build_effective_page_authority(
        page_contract={
            "page_number": bundle["page_number"],
            "body_text": bundle["authoritative_content"]["body_text"],
            "source_tables": source_tables,
        },
        style_execution=execution,
        directives=bundle["resolved_directives"],
        page_images=bundle["page_images"],
        attachment_evidence=bundle["attachment_evidence"],
        search_evidence=bundle["search_evidence"],
    )
    bundle["source_hash"] = hashlib.sha256(bundle["source_text"].encode("utf-8")).hexdigest()
    bundle["effective_page_authority"] = authority
    bundle["required_directives"] = copy.deepcopy(authority["required_directives"])
    bundle["generation_readiness"] = {
        "ready": authority["readiness"]["status"] == "ready",
        "code": "ready",
        "directive_ids": [],
        "blocking_reasons": [],
    }
    bundle["material_summary"] = _material_summary(
        bundle["page_images"], bundle["attachment_evidence"], bundle["search_evidence"],
    )
    bundle["sealed_sha256"] = _seal_digest(bundle)


def _write_generation_inputs(
    tmp_path: Path, *, attachment_text: str | None = None,
) -> tuple[Path, dict, dict]:
    project, source_sha, _ = _project(tmp_path)
    bindings = []
    for asset_id, filename, colour in (
        ("word_asset_001", "chart.png", "white"),
        ("word_asset_002", "photo.png", "blue"),
    ):
        path = project / "00_source" / "word_assets" / "original" / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (32, 18), colour).save(path)
        bindings.append(_binding(
            asset_id=asset_id,
            relative_path=f"00_source/word_assets/original/{filename}",
            sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
            media_type="image/png",
        ))
    if attachment_text is not None:
        original = project / "00_source" / "word_assets" / "original" / "attachment.txt"
        derived = project / "00_source" / "word_assets" / "derived" / "attachment.txt"
        original.parent.mkdir(parents=True, exist_ok=True)
        derived.parent.mkdir(parents=True, exist_ok=True)
        original.write_text(attachment_text, encoding="utf-8")
        derived.write_text(attachment_text, encoding="utf-8")
        bindings.append(_binding(
            asset_id="word_asset_003",
            relative_path="00_source/word_assets/derived/attachment.txt",
            sha256=hashlib.sha256(derived.read_bytes()).hexdigest(),
            media_type="text/plain",
            asset_role="document_source",
        ))
    comments = [
        {"comment_id": "visual-1", "text": "文字表达图片化", "author": "reviewer", "timestamp": None},
        {"comment_id": "visual-2", "text": "采用水墨插画", "author": "reviewer", "timestamp": None},
        {
            "comment_id": "image-1",
            "text": "[require-page-image:word_asset_002]",
            "author": "reviewer",
            "timestamp": None,
        },
    ]
    bundle = _build(project, source_sha, _contract(project, comments=comments, bindings=bindings))
    execution = json.loads((project / "02_style" / "style_execution.json").read_text(encoding="utf-8"))
    return project, bundle, {
        "execution": execution,
        "sha256": hashlib.sha256(canonical_json_bytes(execution)).hexdigest(),
    }


def test_atomic_cutover_makes_v4_the_only_current_project_contract() -> None:
    assert CURRENT_CONTRACT == "word-ppt-workflow-v4"
    assert version_vector() == {
        "workflow_contract_version": "word-ppt-workflow-v4",
        "geometry_contract_version": "fixed-canvas-cm-v2",
        "prompt_contract_version": "page-prompt-v8",
        "qa_policy_version": "risk-qa-v5",
        "reconstruction_version": "editable-image-v3",
        "fixed_layer_version": "native-layer-v3",
    }
    assert require_supported_contract({"workflow_contract_version": "word-ppt-workflow-v4"}) == CURRENT_CONTRACT
    try:
        require_supported_contract({"workflow_contract_version": "word-ppt-workflow-v3"})
    except ValueError as exc:
        assert "word-ppt-workflow-v4" in str(exc)
    else:
        raise AssertionError("V3 must not remain accepted by the production contract boundary")


def test_canonical_prompt_compiles_complete_body_design_from_the_sealed_bundle(tmp_path: Path) -> None:
    project, bundle, style = _write_generation_inputs(tmp_path)
    execution = style["execution"]

    first = compile_page_prompt(bundle, execution, project=project)
    second = compile_page_prompt(bundle, execution, project=project)

    assert first == second
    assert first.startswith("PROMPT_CONTRACT: page-prompt-v8\n")
    assert bundle["source_text"] in first
    assert 'FIXED_PAGE_TITLE_INPUT_ONLY_NOT_RENDERED: "Revenue summary"' in first
    assert (
        "BODY_RENDER_CONTENT: "
        + json.dumps(bundle["authoritative_content"]["body_text"], ensure_ascii=False)
    ) in first
    assert first.index("SOURCE_TEXT_COMPLETE_TRACEABILITY_ONLY:") < first.index(
        "FIXED_PAGE_TITLE_INPUT_ONLY_NOT_RENDERED:"
    ) < first.index("BODY_RENDER_CONTENT:")
    assert "| Revenue | 100 |" in first
    assert "Revenue was 100." in first
    assert '[["Metric","Value"],["Revenue","100"]]' in first
    prompt_contract = json.loads(
        next(
            line.removeprefix("EFFECTIVE_PAGE_VISUAL_CONTRACT: ")
            for line in first.splitlines()
            if line.startswith("EFFECTIVE_PAGE_VISUAL_CONTRACT: ")
        )
    )
    authority_contract = bundle["effective_page_authority"]["effective_visual_contract"]
    assert canonical_json_bytes(prompt_contract) == canonical_json_bytes(authority_contract)
    assert prompt_contract["soft_preferences"]["image_rendering"]["rendering"] == "ink-illustration"
    assert "complete editable-PPT body design" in first
    assert "reference_only" in first and "required_presence" in first
    assert "LEGACY COMMENT MUST NOT APPEAR" not in first
    assert "RAW COMMENT MUST NOT APPEAR" not in first
    assert "page_title" in first
    assert "logo" in first
    assert "footer" in first
    assert "page_number" in first
    assert "VISUAL_BACKGROUND_ONLY" not in first
    assert "do not render any readable text" not in first.casefold()


def test_prompt_sections_encode_comment_over_ui_authority_in_exact_order(tmp_path: Path) -> None:
    project, bundle, style = _write_generation_inputs(tmp_path)
    prompt = compile_page_prompt(bundle, style["execution"], project=project)
    section_names = [
        "AUTHORITY_PRECEDENCE",
        "FIXED_LAYER_EXCLUSIONS",
        "AUTHORITATIVE_WORD_BODY",
        "AUTHORITATIVE_WORD_TABLES",
        "REQUIRED_PAGE_DIRECTIVES",
        "EFFECTIVE_PAGE_VISUAL_CONTRACT",
        "SUPPLIED_PAGE_IMAGES",
        "SUPPLIED_ATTACHMENT_MATERIAL",
        "SUPPLIED_SEARCH_MATERIAL",
    ]

    positions = [prompt.index(f"{name}:") for name in section_names]

    assert positions == sorted(positions)
    assert all(prompt.count(f"{name}:") == 1 for name in section_names)
    assert prompt.count(
        "AUTHORITY_PRECEDENCE: fixed hard rules > Word facts/tables > page comments > "
        "UI global soft style > evidence material > model creativity"
    ) == 1
    assert '"target":"visual.image_ratio"' in prompt
    assert '"value":"medium-high"' in prompt
    assert "Directive text is instruction metadata and must not be rendered" in prompt


def test_prompt_materials_include_verified_trace_fields_and_required_roles(tmp_path: Path) -> None:
    project, bundle, style = _write_generation_inputs(tmp_path)
    prompt = compile_page_prompt(bundle, style["execution"], project=project)

    assert '"asset_id":"word_asset_001"' in prompt
    assert '"relative_path":"00_source/word_assets/original/chart.png"' in prompt
    assert '"presence_role":"reference_only"' in prompt
    assert '"asset_id":"word_asset_002"' in prompt
    assert '"presence_role":"required_presence"' in prompt
    assert '"source_role":"page_image"' in prompt


def test_prompt_refuses_material_blocked_bundles(tmp_path: Path) -> None:
    project, blocked, style = _write_generation_inputs(tmp_path)
    blocked_authority = blocked["effective_page_authority"]
    blocked_authority["readiness"] = {
        "status": "blocked",
        "blocking_reasons": [{
            "code": "required_material_missing",
            "directive_id": "directive-photo",
            "target": "material.page_image",
            "material_id": "photo_2",
        }],
    }
    blocked_authority["sealed_sha256"] = _seal_digest(blocked_authority)
    blocked["generation_readiness"] = {
        "ready": False,
        "code": "required_page_image_unavailable",
        "directive_ids": ["directive-photo"],
        "blocking_reasons": [{
            "code": "required_page_image_unavailable",
            "directive_id": "directive-photo",
            "target": "material.page_image",
            "material_id": "photo_2",
        }],
    }
    blocked["sealed_sha256"] = _seal_digest(blocked)

    with pytest.raises(ValueError, match="signature|seal|generation-ready"):
        compile_page_prompt(blocked, style["execution"], project=project)


def test_initial_request_sends_every_page_image_with_its_exact_trace_role(tmp_path: Path) -> None:
    project, bundle, style = _write_generation_inputs(tmp_path)
    request = page_generation.build_initial_request(
        bundle,
        style,
        project / "06_images" / "generated" / "page_001.png",
        project=project,
    )

    payload = request.payload
    assert request.operation == "edit"
    assert request.endpoint == "images/edits"
    assert payload["image_roles"] == ["reference_only", "required_presence"]
    assert payload["reference_images"] == [
        str((project / "00_source/word_assets/original/chart.png").resolve()),
        str((project / "00_source/word_assets/original/photo.png").resolve()),
    ]
    assert payload["material_bundle_sha256"] == bundle["sealed_sha256"]
    assert payload["prompt_contract_version"] == "page-prompt-v8"
    assert "company_logo" not in json.dumps(payload, ensure_ascii=False)


def test_generation_cache_identity_is_bundle_scoped_and_excludes_fixed_layers() -> None:
    builder = getattr(page_generation, "generation_cache_identity", None)
    assert callable(builder), "V4 generation needs a first-class sealed-bundle cache identity"
    first = builder(
        material_bundle_sha256="1" * 64,
        prompt_sha256="2" * 64,
        generation_parameters={"model": "gpt-image-2", "size": "1904x896", "quality": "high"},
    )
    second = builder(
        material_bundle_sha256="1" * 64,
        prompt_sha256="2" * 64,
        generation_parameters={"model": "gpt-image-2", "size": "1904x896", "quality": "high"},
    )
    assert first == second
    assert set(first) == {
        "workflow_contract_version",
        "prompt_contract_version",
        "material_bundle_sha256",
        "prompt_sha256",
        "generation_parameters",
    }
    assert not {"title_sha256", "logo_sha256", "fixed_layer_version"} & first.keys()


def test_generation_receipt_closes_request_dimensions_roles_mapping_and_trace_identity(tmp_path: Path) -> None:
    project, bundle, style = _write_generation_inputs(tmp_path)
    output = project / "06_images" / "generated" / "page_001.png"
    output.parent.mkdir(parents=True)
    Image.new("RGB", (1904, 896), "white").save(output)
    request = page_generation.build_initial_request(bundle, style, output, project=project)
    trace = output.with_suffix(".trace.json")
    trace.write_text(json.dumps({
        "operation": "edit",
        "endpoint": "images/edits",
        "model": "gpt-image-2",
        "auth": "codex_oauth",
        "input_images": [
            {
                "role": role,
                "path": str(Path(path).resolve()),
                "sha256": hashlib.sha256(Path(path).read_bytes()).hexdigest(),
            }
            for path, role in zip(request.payload["reference_images"], request.payload["image_roles"])
        ],
        "outputs": [{
            "path": str(output.resolve()),
            "sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
        }],
    }) + "\n", encoding="utf-8")

    writer = getattr(page_generation, "write_generation_receipt", None)
    assert callable(writer), "V4 generation results need a closed persisted receipt"
    result = writer(project, bundle, request.payload, output, provider_trace=trace)

    assert result["artifact"]["body_image"] == {
        "path": "06_images/generated/page_001.png",
        "sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
        "width": 1904,
        "height": 896,
    }
    assert result["artifact"]["reference_images"] == [
        {
            "asset_id": "word_asset_001", "evidence_id": None,
            "material_id": "word_asset_001", "source_role": "page_image",
            "sha256": bundle["page_images"][0]["sha256"], "role": "reference_only",
        },
        {
            "asset_id": "word_asset_002", "evidence_id": None,
            "material_id": "word_asset_002", "source_role": "page_image",
            "sha256": bundle["page_images"][1]["sha256"], "role": "required_presence",
        },
    ]
    assert result["artifact"]["effective_authority_sha256"] == bundle["effective_page_authority"]["sealed_sha256"]
    assert result["artifact"]["required_directive_ids"] == [
        item["directive_id"] for item in bundle["required_directives"]
    ]
    assert result["artifact"]["sealed_sha256"]
    assert result["artifact"]["receipt_signature"]
    assert result["artifact"]["request"] == {
        "operation": "edit",
        "endpoint": "images/edits",
        "prompt_sha256": request.prompt_sha256,
        "authority_prompt_sha256": request.authority_prompt_sha256,
        "model": "gpt-image-2",
        "size": "1904x896",
        "quality": "high",
    }
    assert result["artifact"]["body_image_mapping"]["mode"] == "direct"
    assert result["artifact"]["provider_trace"] == {
        "path": trace.relative_to(project).as_posix(),
        "sha256": hashlib.sha256(trace.read_bytes()).hexdigest(),
    }
    assert result["path"].is_file()


def test_aspect_policy_never_contain_passes_an_out_of_tolerance_image() -> None:
    exact = mapping_for_source(2125, 1000)
    near = mapping_for_source(2105, 1000)
    outside = mapping_for_source(2000, 1000)

    assert exact["mode"] == "direct" and exact["image_repair_required"] is False
    assert near["aspect_error"] <= 0.01
    assert near["mode"] == "direct" and near["image_repair_required"] is False
    assert outside["aspect_error"] > 0.01
    assert outside["mode"] == "repair_required"
    assert outside["image_repair_required"] is True
    assert "contain" not in json.dumps(outside)


def test_v4_production_stops_at_a_structured_downstream_pending_stage(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        production_runner.workflow_state,
        "load",
        lambda _project: {
            "style_confirmation": {"status": "confirmed"},
            "final_pptx": None,
        },
    )
    monkeypatch.setattr(
        production_runner.workflow_state,
        "next_action",
        lambda _project: {
            "stage": "qa_backend_pending",
            "workflow_contract_version": "word-ppt-workflow-v4",
            "pending_pages": [1],
            "requests": [],
        },
    )

    result = production_runner.run_production(tmp_path, finalize=False)

    assert result == {
        "stage": "qa_backend_pending",
        "workflow_contract_version": "word-ppt-workflow-v4",
        "pending_pages": [1],
        "requests": [],
        "project": str(tmp_path.resolve()),
        "cycles": 1,
    }


def test_v4_production_modules_expose_no_native_hybrid_or_overlay_contracts() -> None:
    for name in (
        "page_image_references",
        "page_attachment_context",
        "page_comment_instructions",
        "page_qa_contract",
        "reconstruction_target",
    ):
        assert not hasattr(page_pipeline, name)
    assert not hasattr(workflow_state, "record_reconstruction")
