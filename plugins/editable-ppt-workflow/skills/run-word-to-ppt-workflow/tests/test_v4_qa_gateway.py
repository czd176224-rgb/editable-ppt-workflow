from __future__ import annotations

import json
import hashlib
import copy
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import v4_qa  # noqa: E402
import v4_qa_gateway  # noqa: E402
import workflow_state  # noqa: E402
from cache_store import CacheStore  # noqa: E402
from current_contract_fixture import write_valid_generation_receipt  # noqa: E402
from page_material_bundle_v4 import (  # noqa: E402
    _BUNDLE_ATTESTATION_PURPOSE, _bundle_attestation_payload, _material_summary, _seal_digest,
)
from codex_web_material_gateway import sign_project_payload  # noqa: E402
from effective_page_authority import build_effective_page_authority  # noqa: E402
from test_independent_page_workflow import _project  # noqa: E402


def _qa_project(tmp_path: Path, *, white: bool = False) -> tuple[Path, int, Path]:
    project = _project(tmp_path, 1)
    request = workflow_state.next_action(project)["requests"][0]
    attempt = request["attempt"]
    claimed = workflow_state.dispatch(project, 1, "qa-worker", attempt)
    image = Path(claimed["generation_request"]["output"])
    receipt = write_valid_generation_receipt(
        project, 1, attempt, image, size=(34, 16), flat_white=white,
    )
    workflow_state.record_generation(
        project, 1, "qa-worker", attempt, image, generation_receipt=receipt,
    )
    action = workflow_state.next_action(project)
    return project, attempt, project / action["qa_work_items"][0]["path"]


def _service_response(request_payload: dict) -> bytes:
    prompt = json.loads(request_payload["input"][0]["content"][0]["text"])
    decision = {
        "status": "complete",
        "checks": {
            check: {"result": "pass", "detail": "Verified by remote visual review."}
            for check in prompt["check_ids"]
        },
        "required_image_presence": [
            {"asset_id": item["asset_id"], "present": True, "detail": "Visible."}
            for item in prompt["required_presence_images"]
        ],
        "required_directive_results": [
            {"directive_id": item["directive_id"], "satisfied": True, "detail": "Satisfied."}
            for item in prompt["required_directives"]
        ],
    }
    return json.dumps({
        "id": "resp_gateway_test_1",
        "output": [{"content": [{"type": "output_text", "text": json.dumps(decision)}]}],
    }).encode()


def _mock_subscription(monkeypatch, captured: dict | None = None) -> None:
    def invoke(_project, *, prompt: str, images: list[Path], **_kwargs):
        request = json.loads(prompt)
        decision = {
            "status": "complete",
            "checks": {
                check: {"result": "pass", "detail": "Verified by Codex visual review."}
                for check in request["check_ids"]
            },
            "required_image_presence": [
                {"asset_id": item["asset_id"], "present": True, "detail": "Visible."}
                for item in request["required_presence_images"]
            ],
            "required_directive_results": [
                {"directive_id": item["directive_id"], "satisfied": True, "detail": "Satisfied."}
                for item in request["required_directives"]
            ],
        }
        if captured is not None:
            captured.update({"prompt": request, "images": images})
        return SimpleNamespace(value=decision, turn_id="turn_gateway_test_1", model="gpt-test")
    monkeypatch.setattr(v4_qa_gateway, "_invoke_structured", invoke)


def test_production_trust_root_has_no_public_adapter_or_invocation_constructor() -> None:
    assert not hasattr(v4_qa, "TrustedQAProviderAdapter")
    assert not hasattr(v4_qa, "VerifiedQAInvocation")
    assert not hasattr(v4_qa, "ProviderCallResult")


def test_provider_request_uses_authority_directives_not_raw_comment_intents(tmp_path: Path) -> None:
    project, _attempt, work_path = _qa_project(tmp_path)
    work = json.loads(work_path.read_text(encoding="utf-8"))
    prompt = json.loads(v4_qa._provider_request(work))

    assert prompt["effective_page_authority_sha256"] == work["effective_page_authority_sha256"]
    assert prompt["required_directives"] == work["required_directives"]
    assert "comment_intents" not in prompt


def test_page2_word_facts_remain_authoritative_when_visualization_changes(tmp_path: Path) -> None:
    project, _attempt, work_path = _qa_project(tmp_path)
    work = json.loads(work_path.read_text(encoding="utf-8"))
    source = "聚焦三地市场，三种增长模式并举，实现利润10%增长。"
    work["authoritative_content"]["source_text"] = source
    work["authoritative_content"]["body_text"] = source
    work["required_directives"] = [{
        "directive_id": "page2-visual", "target": "visual.layout",
        "action": "set", "value": "infographic",
    }]

    prompt = json.loads(v4_qa._provider_request(work))

    assert prompt["authoritative_content"]["source_text"] == source
    assert prompt["required_directives"] == work["required_directives"]
    assert prompt["review_instructions"]["key_facts_preserved"].startswith("Compare the body image")


def test_page3_required_news_image_is_evaluated_against_exact_search_pixels(
    tmp_path: Path, monkeypatch,
) -> None:
    project = tmp_path / "project"
    pixels = project / "03_evidence" / "page_003" / "search" / "news.png"
    pixels.parent.mkdir(parents=True)
    Image.new("RGB", (20, 10), "#224466").save(pixels)
    digest = hashlib.sha256(pixels.read_bytes()).hexdigest()
    material_id = "search-request-0123456789abcdef"
    bundle = {"required_directives": [{
        "directive_id": "page3-news", "target": "material.search_evidence",
        "action": "require", "material_id": material_id,
    }]}
    monkeypatch.setattr(v4_qa, "verified_reference_inputs", lambda *_args, **_kwargs: ({
        "path": str(pixels), "relative_path": pixels.relative_to(project).as_posix(),
        "sha256": digest, "presence_role": "required_presence",
        "source_role": "search_evidence", "asset_id": material_id,
        "evidence_id": "news-pixel-1", "material_id": material_id,
    },))

    records = v4_qa._qa_image_records(project, bundle, {})

    assert records == [{
        "asset_id": "news-pixel-1", "source_asset_id": material_id,
        "path": pixels.relative_to(project).as_posix(), "evidence_id": "news-pixel-1",
        "material_id": material_id, "source_role": "search_evidence", "sha256": digest,
        "media_type": "image/png", "width": 20, "height": 10,
        "presence_policy": "required_presence", "directive_ids": ["page3-news"],
    }]


def test_required_material_missing_is_upstream_state_error_before_qa(
    tmp_path: Path, monkeypatch,
) -> None:
    bundle = {"required_directives": [{
        "directive_id": "page3-news", "target": "material.search_evidence",
        "action": "require", "material_id": "search-request-0123456789abcdef",
    }]}
    monkeypatch.setattr(v4_qa, "verified_reference_inputs", lambda *_args, **_kwargs: ())
    with pytest.raises(ValueError, match="upstream state error"):
        v4_qa._qa_image_records(tmp_path, bundle, {})


def test_builtin_gateway_signs_and_state_consumes_once(tmp_path: Path, monkeypatch) -> None:
    project, attempt, work = _qa_project(tmp_path)
    _mock_subscription(monkeypatch)
    bundle = v4_qa_gateway.invoke_builtin_gateway(project, work, timeout=2)

    accepted = workflow_state.record_qa(
        project, 1, "qa-worker", attempt, signed_invocation_bundle=bundle,
    )
    assert accepted["state"] == "accepted"
    with pytest.raises(ValueError, match="nonce|consumed|replay"):
        workflow_state.verify_signed_qa_bundle(project, bundle, consume=True)


@pytest.mark.parametrize(
    "mutation",
    ["soft_extra", "hard_extra", "creative_extra", "missing_required", "leaf_wrong_type"],
)
def test_gateway_strictly_rejects_resealed_invalid_nested_visual_contract_before_model_call(
    tmp_path: Path, monkeypatch, mutation: str,
) -> None:
    project, _attempt, work_path = _qa_project(tmp_path)
    work = json.loads(work_path.read_text(encoding="utf-8"))
    visual = copy.deepcopy(work["visual_contract"])
    if mutation == "soft_extra":
        visual["soft_preferences"]["unexpected"] = True
    elif mutation == "hard_extra":
        visual["hard_constraints"]["unexpected"] = True
    elif mutation == "creative_extra":
        visual["creative_freedom"]["unexpected"] = True
    elif mutation == "missing_required":
        visual["hard_constraints"].pop("title_color")
    else:
        visual["hard_constraints"]["title_color"] = 7
    work["visual_contract"] = visual
    work["sealed_sha256"] = v4_qa._seal(work)
    work_path.write_text(
        json.dumps(work, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    calls = 0

    def invoke(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        raise AssertionError("model must not be called for an invalid visual contract")

    monkeypatch.setattr(v4_qa_gateway, "_invoke_structured", invoke)
    with pytest.raises(ValueError, match="visual contract"):
        v4_qa_gateway.invoke_builtin_gateway(project, work_path, timeout=2)
    assert calls == 0


@pytest.mark.parametrize(
    ("field", "forged"),
    [
        ("provider", "forged"), ("model", "forged"), ("endpoint", "https://evil.invalid/v1"),
        ("dry_run", True), ("nonce", "0" * 48), ("timestamp", "2020-01-01T00:00:00Z"),
        ("qa_work_item_sha256", "0" * 64), ("request_sha256", "0" * 64),
        ("body_image_sha256", "0" * 64), ("reference_image_sha256s", ["0" * 64]),
        ("raw_response_sha256", "0" * 64), ("service_request_id", "forged-request"),
    ],
)
def test_any_unsigned_attestation_mutation_is_rejected_without_state_change(
    tmp_path: Path, monkeypatch, field: str, forged,
) -> None:
    project, attempt, work = _qa_project(tmp_path)
    _mock_subscription(monkeypatch)
    bundle = v4_qa_gateway.invoke_builtin_gateway(project, work, timeout=2)
    value = json.loads(bundle.read_text(encoding="utf-8"))
    value["attestation"][field] = forged
    bundle.write_text(json.dumps(value) + "\n", encoding="utf-8")

    with pytest.raises(ValueError, match="signed invocation|validation"):
        workflow_state.record_qa(
            project, 1, "qa-worker", attempt, signed_invocation_bundle=bundle,
        )
    job = workflow_state.load(project)["jobs"][0]
    assert job["status"] == "qa"
    assert job.get("automatic_repairs_used", 0) == 0


def test_white_image_cannot_pass_even_when_service_returns_all_pass(tmp_path: Path, monkeypatch) -> None:
    project, attempt, work = _qa_project(tmp_path, white=True)
    _mock_subscription(monkeypatch)
    bundle = v4_qa_gateway.invoke_builtin_gateway(project, work, timeout=2)
    result = workflow_state.record_qa(
        project, 1, "qa-worker", attempt, signed_invocation_bundle=bundle,
    )
    assert result["state"] == "repair"
    assert any(issue["code"] == "gross_content_missing" for issue in result["qa_result"]["issues"])


def test_legacy_api_provider_settings_do_not_change_subscription_transport(tmp_path: Path, monkeypatch) -> None:
    project, _attempt, work = _qa_project(tmp_path)
    _mock_subscription(monkeypatch)
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-be-used")
    monkeypatch.setenv("EDITABLE_PPT_QA_PROVIDER", "arbitrary")
    bundle = json.loads(v4_qa_gateway.invoke_builtin_gateway(project, work, timeout=2).read_text(encoding="utf-8"))
    assert bundle["attestation"]["provider"] == "codex-chatgpt"
    assert bundle["attestation"]["endpoint"] == "codex-app-server"


def test_required_presence_source_image_bytes_and_order_are_bound_to_gateway_request(
    tmp_path: Path, monkeypatch,
) -> None:
    project = _project(tmp_path, 1)
    asset = project / "00_source" / "word_assets" / "required.png"
    asset.parent.mkdir(parents=True)
    Image.new("RGB", (12, 8), "#2A568C").save(asset)
    digest = hashlib.sha256(asset.read_bytes()).hexdigest()
    state_path = project / "workflow_run.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    job = state["jobs"][0]
    material_path = project / job["material_bundle_file"]
    material = json.loads(material_path.read_text(encoding="utf-8"))
    material["page_images"] = [{
        "asset_id": "required-chart", "path": asset.relative_to(project).as_posix(),
        "sha256": digest, "media_type": "image/png", "presence_policy": "required_presence",
        "promotion": {
            "source": "page_comment", "directive_type": "require_page_image",
            "asset_id": "required-chart", "raw": "[require-page-image:required-chart]",
        },
    }]
    material["required_presence_asset_ids"] = ["required-chart"]
    contract = json.loads((project / job["contract_file"]).read_text(encoding="utf-8"))
    style = json.loads((project / material["style_execution"]["path"]).read_text(encoding="utf-8"))
    material["effective_page_authority"] = build_effective_page_authority(
        page_contract=contract, style_execution=style, directives=[],
        page_images=material["page_images"], attachment_evidence=[], search_evidence=[],
    )
    material["required_directives"] = []
    material["material_summary"] = _material_summary(material["page_images"], [], [])
    material["bundle_attestation_signature"] = sign_project_payload(
        project, _bundle_attestation_payload(material), purpose=_BUNDLE_ATTESTATION_PURPOSE,
    )
    material["sealed_sha256"] = _seal_digest(material)
    material_path.write_text(json.dumps(material, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    job["material_bundle_sha256"] = material["sealed_sha256"]
    job["material_bundle_file_sha256"] = hashlib.sha256(material_path.read_bytes()).hexdigest()
    state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    request = workflow_state.next_action(project)["requests"][0]
    attempt = request["attempt"]
    claimed = workflow_state.dispatch(project, 1, "qa-worker", attempt)
    body = Path(claimed["generation_request"]["output"])
    receipt = write_valid_generation_receipt(project, 1, attempt, body)
    workflow_state.record_generation(project, 1, "qa-worker", attempt, body, generation_receipt=receipt)
    action = workflow_state.next_action(project)
    work_path = project / action["qa_work_items"][0]["path"]
    captured: dict = {}

    _mock_subscription(monkeypatch, captured)
    bundle_path = v4_qa_gateway.invoke_builtin_gateway(project, work_path, timeout=2)
    prompt = captured["prompt"]

    assert prompt["required_presence_images"] == [{
        "asset_id": "required-chart", "path": asset.relative_to(project).as_posix(),
        "source_asset_id": "required-chart", "evidence_id": None,
        "material_id": "required-chart", "source_role": "page_image",
        "sha256": digest, "media_type": "image/png", "width": 12, "height": 8,
        "presence_policy": "required_presence", "directive_ids": [],
    }]
    assert captured["images"] == [body.resolve(), asset.resolve()]
    bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
    assert bundle["attestation"]["reference_image_sha256s"] == [digest]
    accepted = workflow_state.record_qa(
        project, 1, "qa-worker", attempt, signed_invocation_bundle=bundle_path,
    )
    assert accepted["state"] == "accepted"
    cache = workflow_state.load(project)["jobs"][0]["generation_cache"]
    hit = CacheStore(project).lookup("generations", cache["key"])
    assert hit is not None
    assert asset.relative_to(project).as_posix() in hit.manifest["logical_files"]
