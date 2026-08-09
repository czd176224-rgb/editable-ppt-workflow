"""One batched Codex-subscription review of final reconstructed slide previews."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

from PIL import Image

from codex_subscription_runtime import invoke_structured
from fixed_region_contract import BODY_BOX_CM, SLIDE_SIZE_CM
from workflow_v5_final_qa import (
    build_final_qa_batches,
    decide_final_qa,
    normalize_semantic_findings,
)
from workflow_v5_request_ledger import RequestLedger


def _schema(page_numbers: list[int]) -> dict[str, Any]:
    finding = {
        "type": "object", "additionalProperties": False,
        "required": ["issue_type", "requirement_class", "level", "owner", "message"],
        "properties": {
            "issue_type": {"enum": [
                "missing_word_content", "incorrect_word_content", "unreadable_required_content",
                "page_comment_unfulfilled", "required_authentic_material_not_visible",
                "accepted_design_fidelity_mismatch", "minor_text_overlap", "small_text",
                "layout_density", "logo_text_collision", "style_polish", "readability",
                "other_advisory"
            ]},
            "requirement_class": {"enum": ["hard", "soft"]},
            "level": {"enum": ["blocking", "advisory"]},
            "owner": {"enum": ["design", "material", "compose", "reconstruct", "assemble"]},
            "message": {"type": "string", "minLength": 1},
        },
    }
    score = {
        "type": "number", "minimum": 1, "maximum": 5,
        "description": "5=excellent, 4=good, 3=usable with visible issues, 2=poor, 1=unusable",
    }
    return {
        "type": "object", "additionalProperties": False,
        "required": ["pages"],
        "properties": {"pages": {
            "type": "array", "minItems": len(page_numbers), "maxItems": len(page_numbers),
            "items": {
                "type": "object", "additionalProperties": False,
                "required": ["page_number", "findings", "scores"],
                "properties": {
                    "page_number": {"enum": page_numbers},
                    "findings": {"type": "array", "items": finding, "maxItems": 8},
                    "scores": {
                        "type": "object", "additionalProperties": False,
                        "required": ["content_fidelity", "style_consistency", "readability", "reconstruction_fidelity"],
                        "properties": {
                            "content_fidelity": score,
                            "style_consistency": score,
                            "readability": score,
                            "reconstruction_fidelity": score,
                        },
                    },
                },
            },
        }},
    }


def _design_reference(project: Path, state: dict[str, Any], page: int) -> Path:
    compose_receipt = project / "04_v5" / "compose" / f"page_{page:03d}.json"
    if compose_receipt.is_file():
        compose = json.loads(compose_receipt.read_text(encoding="utf-8"))
        composed = compose.get("composed_body")
        if isinstance(composed, Mapping):
            path = project / str(composed.get("path", ""))
            if path.is_file():
                actual = "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
                if actual != composed.get("artifact_id"):
                    raise ValueError("accepted composed body changed after composition")
                with Image.open(path) as image:
                    if image.size != (1904, 896):
                        raise ValueError("accepted composed body must be exactly 1904x896")
                return path
    receipt = project / "04_v5" / "design" / f"page_{page:03d}.json"
    if receipt.is_file():
        value = json.loads(receipt.read_text(encoding="utf-8"))
        path = project / value["output"]
    else:
        job = next(item for item in state["jobs"] if item["page_number"] == page)
        generation = job.get("generation") or {}
        path = project / generation["image"]
    with Image.open(path) as image:
        if image.size != (1904, 896):
            raise ValueError("accepted Image2 visual reference must be exactly 1904x896")
    return path


def _crop_final_body(slide_render: Path, output: Path) -> Path:
    """Crop the canonical editable body from a full-slide render and normalize it for QA."""
    source = Path(slide_render)
    target = Path(output)
    with Image.open(source) as opened:
        image = opened.convert("RGB")
    width, height = image.size
    expected_ratio = SLIDE_SIZE_CM["w"] / SLIDE_SIZE_CM["h"]
    if width < 1 or height < 1 or abs((width / height) - expected_ratio) > 0.01:
        raise ValueError("final QA preview must be a full-slide render with canonical aspect ratio")
    left = round(width * BODY_BOX_CM["x"] / SLIDE_SIZE_CM["w"])
    top = round(height * BODY_BOX_CM["y"] / SLIDE_SIZE_CM["h"])
    right = round(
        width * (BODY_BOX_CM["x"] + BODY_BOX_CM["w"]) / SLIDE_SIZE_CM["w"]
    )
    bottom = round(
        height * (BODY_BOX_CM["y"] + BODY_BOX_CM["h"]) / SLIDE_SIZE_CM["h"]
    )
    if not (0 <= left < right <= width and 0 <= top < bottom <= height):
        raise ValueError("canonical final QA body crop is outside the slide render")
    body = image.crop((left, top, right, bottom)).resize(
        (1904, 896), Image.Resampling.LANCZOS,
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.stem}.tmp{target.suffix}")
    body.save(temporary, format="PNG")
    temporary.replace(target)
    return target


def _page_payload(project: Path, page: int) -> tuple[dict[str, Any], Path, Path]:
    contract = json.loads(
        (project / "01_page_contracts" / f"page_{page:03d}.json").read_text(encoding="utf-8")
    )
    state = json.loads((project / "workflow_run.json").read_text(encoding="utf-8"))
    style = json.loads(
        (project / state["style_confirmation"]["execution_file"]).read_text(encoding="utf-8")
    )
    intent = json.loads(
        (project / "04_v5" / "intents" / f"page_{page:03d}.json").read_text(encoding="utf-8")
    )
    preview = project / "04_v5" / "final-pages" / f"page_{page:03d}" / "rendered" / "slide_001.png"
    receipt_path = project / "04_v5" / "final-pages" / f"page_{page:03d}" / "final-page.json"
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    page_pptx = project / receipt["page_pptx"]
    expected_preview = project / receipt["preview"]
    if expected_preview.resolve() != preview.resolve():
        raise ValueError("final QA preview path does not match finalization receipt")
    for path, field in (
        (page_pptx, "page_artifact_id"), (preview, "preview_artifact_id"),
    ):
        actual = "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
        if actual != receipt.get(field):
            raise ValueError("final QA artifact changed after finalization")
    body_comparison = preview.parent / "body-comparison.png"
    _crop_final_body(preview, body_comparison)
    payload = {
        "page_number": page,
        "source_text": contract["source_text"],
        "page_comments": [item["text"] for item in contract.get("page_comments", [])],
        "hard_requirements": [
            item["description"] for item in intent.get("material_requirements", [])
            if item.get("required") is True
        ],
        "soft_style": {
            "visual_contract": style.get("visual_contract", style.get("execution", {})),
            "note": "Only explicit Word facts/comments are blocking; style/readability improvements are advisory unless unusable.",
        },
        "visual_fidelity_contract": (
            "The final editable body must closely match the accepted composed body in composition, "
            "layout, hierarchy, palette, visual rhythm, and major decorative elements. Exact required "
            "authentic source pixels may replace an Image2-imagined lookalike. Fixed title, logo, footer, "
            "and page number are outside the body comparison. A material design mismatch is blocking."
        ),
    }
    return payload, _design_reference(project, state, page), body_comparison


def _normalize_and_validate_pages(pages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized_pages: list[dict[str, Any]] = []
    for raw_page in pages:
        page = dict(raw_page)
        findings = normalize_semantic_findings(page.get("findings", []))
        scores = page.get("scores", {})
        values = [scores.get(name) for name in (
            "content_fidelity", "style_consistency", "readability", "reconstruction_fidelity",
        )]
        if any(not isinstance(value, (int, float)) for value in values):
            raise ValueError("final QA scores are incomplete")
        if scores["reconstruction_fidelity"] < 4:
            mismatch = next((
                item for item in findings
                if item["issue_type"] == "accepted_design_fidelity_mismatch"
            ), None)
            findings = [
                item for item in findings
                if item["issue_type"] != "accepted_design_fidelity_mismatch"
            ]
            findings.append({
                "issue_type": "accepted_design_fidelity_mismatch",
                "requirement_class": "hard",
                "level": "blocking",
                "owner": "reconstruct",
                "message": (
                    mismatch["message"] if mismatch is not None else
                    "Reconstruction fidelity is below 4/5 against the accepted composed body; "
                    "perform one targeted editable reconstruction repair."
                ),
            })
        blocking = any(
            item["requirement_class"] == "hard" and item["level"] == "blocking"
            for item in findings
        )
        blocking_types = {
            item["issue_type"] for item in findings
            if item["requirement_class"] == "hard" and item["level"] == "blocking"
        }
        if scores["content_fidelity"] <= 2 and not blocking_types.intersection({
            "missing_word_content", "incorrect_word_content", "page_comment_unfulfilled",
            "required_authentic_material_not_visible",
        }):
            raise ValueError("final QA scores contradict missing content-fidelity finding")
        if scores["readability"] <= 2 and "unreadable_required_content" not in blocking_types:
            raise ValueError("final QA severe score requires an unreadable-content blocking finding")
        if (
            scores["style_consistency"] <= 2
            and "accepted_design_fidelity_mismatch" not in blocking_types
        ):
            raise ValueError("final QA severe score requires a design-fidelity blocking finding")
        if not findings and min(values) < 4:
            raise ValueError("final QA scores contradict an issue-free decision")
        if min(values) <= 2 and not blocking:
            raise ValueError("final QA severe score requires a blocking finding")
        page["findings"] = findings
        normalized_pages.append(page)
    return normalized_pages


def run_final_qa(
    project: Path, *, page_numbers: list[int], timeout: float = 900,
    automatic_repairs_used_by_page: Mapping[int, int] | None = None,
) -> dict[str, Any]:
    root = Path(project).resolve()
    repair_counts = dict(automatic_repairs_used_by_page or {})
    if set(repair_counts) - set(page_numbers) or any(
        type(page) is not int or type(count) is not int or count not in {0, 1}
        for page, count in repair_counts.items()
    ):
        raise ValueError("final QA automatic repair counts are invalid")
    prepared = [_page_payload(root, page) for page in page_numbers]
    pages = [item[0] for item in prepared]
    reference_by_page = {payload["page_number"]: reference for payload, reference, _preview in prepared}
    preview_by_page = {payload["page_number"]: preview for payload, _reference, preview in prepared}
    batches = build_final_qa_batches(pages, profile="balanced")
    ledger = RequestLedger(root)
    returned = []
    invocation_reports = []
    for index, batch in enumerate(batches, start=1):
        numbers = [item["page_number"] for item in batch["pages"]]
        images = [
            path for page in numbers
            for path in (reference_by_page[page], preview_by_page[page])
        ]
        semantic_inputs = {
            "batch": batch,
            "image_pair_sha256": [hashlib.sha256(path.read_bytes()).hexdigest() for path in images],
            "body_comparison_artifacts": [{
                "page_number": page,
                "artifact_location": preview_by_page[page].relative_to(root).as_posix(),
                "sha256": "sha256:" + hashlib.sha256(preview_by_page[page].read_bytes()).hexdigest(),
            } for page in numbers],
            "policy": "accepted-composed-body-vs-final-body-balanced-v7",
        }
        worker = f"final-qa-batch:{index}"
        claim = ledger.claim("final_slide_qa", semantic_inputs, worker_id=worker)
        if claim["decision"] == "busy":
            raise ValueError("final QA batch is already running")
        if claim["decision"] == "reuse":
            cached = claim["result"]
            if not isinstance(cached, dict) or not isinstance(cached.get("pages"), list):
                raise ValueError("cached final QA result is invalid")
            batch_report = cached
        else:
            prompt = (
                "Review image pairs in page order: for each page, the first image is the accepted composed "
                "body design and the second image is the deterministic canonical body crop from the final "
                "editable PowerPoint slide, normalized to the same 1904x896 canvas. Compare body to body; "
                "fixed title, logo, footer, and page number are intentionally excluded. "
                "The corresponding page contracts follow as JSON. Block only an explicit Word fact, "
                "table, page comment, or required authentic-photo requirement that is materially missing, "
                "wrong, or unreadable. Do not block for optional polish, preferred aesthetics, minor spacing, "
                "or internal file/provenance concerns. Reconstruction fidelity below 4 is blocking and must "
                "use accepted_design_fidelity_mismatch owned by reconstruct. Classify small but readable text "
                "and other soft style/readability suggestions as advisory. Score every dimension with "
                "5=excellent, 4=good, 3=usable with visible issues, "
                "2=poor, and 1=unusable. Scores must agree with findings: an issue-free page cannot score "
                "below 4, and a score of 1 or 2 requires a blocking finding. Authenticity itself is checked "
                "deterministically; assess only whether the requested "
                "real photo is visibly and appropriately placed. Return every requested page exactly once.\n"
                + json.dumps(batch, ensure_ascii=False)
            )
            try:
                result = invoke_structured(
                    root, role="final-slide-qa", prompt=prompt, images=images,
                    output_schema=_schema(numbers), timeout=timeout,
                )
                normalized_pages = _normalize_and_validate_pages(result.value.get("pages", []))
                batch_report = {
                    "pages": normalized_pages, "model": result.model,
                    "model_provider": result.model_provider, "effort": result.effort,
                    "auth_mode": result.auth_mode, "usage": dict(result.usage),
                    "thread_id": result.thread_id, "turn_id": result.turn_id,
                }
                ledger.complete_success(
                    claim["request_key"], worker_id=worker, result=batch_report,
                )
            except Exception as exc:
                ledger.fail_retryable(claim["request_key"], worker_id=worker, reason=str(exc))
                raise
        if sorted(item["page_number"] for item in batch_report["pages"]) != sorted(numbers):
            raise ValueError("final QA batch did not return every page exactly once")
        returned.extend(batch_report["pages"])
        invocation_reports.append({
            key: batch_report.get(key)
            for key in ("model", "model_provider", "effort", "auth_mode", "usage", "thread_id", "turn_id")
        })
    if sorted(item["page_number"] for item in returned) != sorted(page_numbers):
        raise ValueError("final QA did not return every page exactly once")
    decisions = []
    for page in returned:
        decision = decide_final_qa(
            deterministic_findings=[], semantic_findings=page["findings"],
            automatic_repairs_used=repair_counts.get(page["page_number"], 0),
        )
        decisions.append({**page, "decision": decision})
    report = {
        "artifact_version": "v5-final-qa-report-v1",
        "review_target": "accepted_composed_body_vs_final_editable_pair",
        "pages": decisions,
        "model": invocation_reports[0].get("model"),
        "model_provider": invocation_reports[0].get("model_provider"),
        "effort": invocation_reports[0].get("effort"),
        "auth_mode": invocation_reports[0].get("auth_mode"),
        "usage": invocation_reports[0].get("usage") or {},
        "thread_id": invocation_reports[0].get("thread_id"),
        "turn_id": invocation_reports[0].get("turn_id"),
        "batches": invocation_reports,
        "blocking_pages": [
            item["page_number"] for item in decisions
            if item["decision"]["action"] != "deliver"
        ],
    }
    output = root / "09_reports" / "v5_final_qa.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report
