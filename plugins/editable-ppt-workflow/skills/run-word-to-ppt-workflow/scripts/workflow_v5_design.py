"""Generate and semantically accept one 1904x896 Image2 body through Codex OAuth."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import uuid
import xml.etree.ElementTree as ET
from dataclasses import replace
from pathlib import Path
from typing import Any, Mapping

from PIL import Image

from page_generation import build_initial_request
from workflow_v5_design_qa import review_image2_design
from workflow_v5_final_qa import deterministic_image2_preflight
from workflow_v5_identity import ContentCatalog
from workflow_v5_request_ledger import RequestLedger
from workflow_v5_asset_slots import (
    build_asset_slot_plan,
    required_reference_assets,
    slot_plan_identity,
)
from workflow_v5_compose import compose_candidate_body


IMAGE_CLI = (
    Path(__file__).resolve().parents[2]
    / "generate-slide-body-image" / "scripts" / "codex_gpt_image.py"
)
_ACCEPTANCE_POLICY = "compose-owned-authentic-panel-acceptance-v8"


class DesignAcceptanceBlocked(RuntimeError):
    """Terminal quality failure after the one allowed semantic repair."""


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _run_image(command: list[str], *, timeout: int, label: str) -> None:
    completed = subprocess.run(
        command, capture_output=True, text=True, errors="replace",
        timeout=timeout, check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError((completed.stderr or completed.stdout or f"{label} failed")[-1200:])


def _verify_image(path: Path) -> tuple[int, int]:
    try:
        with Image.open(path) as image:
            image.verify()
        with Image.open(path) as image:
            return image.size
    except (OSError, ValueError) as exc:
        raise ValueError("Image2 output is unreadable") from exc


def _project_file(root: Path, value: Any, *, label: str) -> Path:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} path is missing")
    relative = Path(value)
    if relative.is_absolute():
        raise ValueError(f"{label} path must be project-relative")
    path = (Path(root).resolve() / relative).resolve()
    try:
        path.relative_to(Path(root).resolve())
    except ValueError as exc:
        raise ValueError(f"{label} escapes the project") from exc
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"{label} is unavailable")
    return path


def _fixed_logo_reference(root: Path, logo_source: Mapping[str, Any]) -> dict[str, Any]:
    """Build a visual-safe fixed-logo identity; SVG bytes are never attached as an image."""
    if not isinstance(logo_source, Mapping):
        raise ValueError("fixed logo source is missing")
    svg = _project_file(root, logo_source.get("path"), label="fixed logo SVG")
    if svg.suffix.lower() != ".svg" or logo_source.get("media_type") not in {None, "image/svg+xml"}:
        raise ValueError("fixed logo source must be SVG")
    svg_sha = _sha256_file(svg)
    if logo_source.get("sha256") != svg_sha:
        raise ValueError("fixed logo SVG SHA-256 mismatch")
    labels: list[str] = []
    try:
        tree = ET.parse(svg)
        for element in tree.getroot().iter():
            local = element.tag.rsplit("}", 1)[-1].lower()
            if local in {"title", "desc", "text"}:
                text = " ".join("".join(element.itertext()).split())
                if text and text not in labels:
                    labels.append(text)
    except (ET.ParseError, OSError):
        # The upstream fixed-frame validator owns full SVG renderability. The
        # acceptance gate still has the sealed filename and exact content hash.
        labels = []
    stem = " ".join(svg.stem.replace("_", " ").replace("-", " ").split())
    semantic_description = (
        f"Exact fixed association logo/wordmark identity: source filename '{svg.name}', "
        f"name terms '{stem}'"
        + (f", embedded labels '{' | '.join(labels[:8])}'" if labels else "")
        + ". Any matching association wordmark, emblem, logo-like mark, or title/header branding is forbidden."
    )
    result: dict[str, Any] = {
        "mode": "semantic_description_only",
        "svg_sha256": svg_sha,
        "semantic_description": semantic_description,
        "preview_path": None,
        "preview_sha256": None,
    }
    preview = logo_source.get("raster_preview")
    if preview is None:
        return result
    if not isinstance(preview, Mapping):
        raise ValueError("fixed logo raster preview record is invalid")
    if preview.get("source_svg_sha256") != svg_sha:
        raise ValueError("fixed logo raster preview source SVG closure mismatch")
    preview_path = _project_file(root, preview.get("path"), label="fixed logo raster preview")
    actual_sha = _sha256_file(preview_path)
    if preview.get("sha256") != actual_sha:
        raise ValueError("fixed logo raster preview SHA-256 mismatch")
    expected_formats = {
        "image/png": "PNG", "image/jpeg": "JPEG", "image/webp": "WEBP",
    }
    expected_format = expected_formats.get(str(preview.get("media_type")))
    if expected_format is None:
        raise ValueError("fixed logo raster preview media type is unsupported")
    try:
        with Image.open(preview_path) as opened:
            opened.verify()
        with Image.open(preview_path) as opened:
            if opened.format != expected_format:
                raise ValueError("fixed logo raster preview format mismatch")
    except OSError as exc:
        raise ValueError("fixed logo raster preview is unreadable") from exc
    result.update({
        "mode": "verified_raster_preview",
        "preview_path": str(preview_path),
        "preview_sha256": actual_sha,
    })
    return result


def _logo_reference_identity(reference: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "mode": reference["mode"],
        "svg_sha256": reference["svg_sha256"],
        "preview_sha256": reference.get("preview_sha256"),
        "semantic_description_sha256": hashlib.sha256(
            str(reference["semantic_description"]).encode("utf-8")
        ).hexdigest(),
    }


def _prompt_json_field(prompt: str, label: str) -> str:
    prefix = label + ": "
    line = next((item for item in prompt.splitlines() if item.startswith(prefix)), None)
    if line is None:
        raise ValueError(f"Image2 prompt is missing {label}")
    try:
        value = json.loads(line[len(prefix):])
    except json.JSONDecodeError as exc:
        raise ValueError(f"Image2 prompt {label} is invalid") from exc
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Image2 prompt {label} is empty")
    return value


def _reference_inputs(request: Any) -> list[dict[str, Any]]:
    return [{
        "path": str(path), "presence_role": presence_role, "source_role": source_role,
        "asset_id": asset_id, "evidence_id": evidence_id, "material_id": material_id,
        "sha256": sha256,
    } for (
        path, presence_role, source_role, asset_id, evidence_id, material_id, sha256
    ) in request.reference_images]


def _qa_invocation(value: Mapping[str, Any], *, attempt: int) -> dict[str, Any]:
    if type(value.get("accepted")) is not bool or not isinstance(value.get("issues"), list):
        raise ValueError("Image2 semantic QA result is invalid")
    return {
        "attempt": attempt,
        "outcome": "pass" if value["accepted"] else "fail",
        "checks": value.get("checks", {}),
        "issues": [dict(item) for item in value["issues"]],
        "model": value.get("model"),
        "model_provider": value.get("model_provider"),
        "effort": value.get("effort"),
        "auth_mode": value.get("auth_mode"),
        "usage": dict(value.get("usage") or {}),
        "thread_id": value.get("thread_id"),
        "turn_id": value.get("turn_id"),
    }


def _semantic_repair_text(prompt: str, issues: list[Mapping[str, Any]]) -> str:
    targeted = [{
        "issue_type": item.get("issue_type"), "message": item.get("message"),
    } for item in issues]
    return (
        prompt
        + "\nSEMANTIC_REPAIR_ONLY: Perform exactly one issue-targeted edit of the supplied candidate. "
          "Correct every issue below while preserving all already-correct Word facts, required materials, "
          "directives, hierarchy, palette, composition quality, and exact 1904x896 body geometry. Do not "
          "add the fixed page title, association logo, footer line, or page number. Do not introduce new facts.\n"
        + json.dumps(targeted, ensure_ascii=False, sort_keys=True)
    )


def _review_with_ledger(
    ledger: RequestLedger,
    *,
    root: Path,
    image: Path,
    logo_reference: Mapping[str, Any],
    page_number: int,
    page_title: str,
    bundle: Mapping[str, Any],
    references: list[dict[str, Any]],
    authority_sha256: str,
    timeout: int,
) -> dict[str, Any]:
    image_sha = _sha256_file(image)
    inputs = {
        "policy": _ACCEPTANCE_POLICY,
        "page_number": page_number,
        "candidate_sha256": image_sha,
        "fixed_logo_reference": _logo_reference_identity(logo_reference),
        "page_title_sha256": hashlib.sha256(page_title.encode("utf-8")).hexdigest(),
        "material_bundle_sha256": authority_sha256,
        "reference_inputs": [{
            key: item.get(key) for key in (
                "presence_role", "source_role", "asset_id", "evidence_id", "material_id", "sha256"
            )
        } for item in references],
    }
    qa_worker = f"image2-design-qa:{page_number}:{image_sha[:16]}"
    claim = ledger.claim("image2_design_qa", inputs, worker_id=qa_worker)
    if claim["decision"] == "busy":
        raise ValueError("equivalent Image2 design QA request is already running")
    if claim["decision"] == "reuse":
        cached = claim.get("result")
        if not isinstance(cached, dict):
            raise ValueError("cached Image2 design QA result is invalid")
        return cached
    try:
        result = review_image2_design(
            root, image=image, fixed_logo_reference=logo_reference, page_number=page_number,
            page_title=page_title, material_bundle=bundle, reference_inputs=references,
            timeout=timeout,
        )
        _qa_invocation(result, attempt=1)
        ledger.complete_success(claim["request_key"], worker_id=qa_worker, result=result)
        return result
    except Exception as exc:
        ledger.fail_retryable(claim["request_key"], worker_id=qa_worker, reason=str(exc))
        raise


def generate_v5_design(project: Path, *, page_number: int, timeout: int = 900) -> dict[str, Any]:
    root = Path(project).resolve()
    state = json.loads((root / "workflow_run.json").read_text(encoding="utf-8"))
    job = next(item for item in state["jobs"] if item["page_number"] == page_number)
    bundle_path = root / job["material_bundle_file"]
    bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
    style_path = root / state["style_confirmation"]["execution_file"]
    style_execution = json.loads(style_path.read_text(encoding="utf-8"))
    style = {
        "execution": style_execution,
        "sha256": state["style_confirmation"]["execution_sha256"],
    }
    logo_reference = _fixed_logo_reference(root, state["logo_source"])

    directory = root / "04_v5" / "design"
    directory.mkdir(parents=True, exist_ok=True)
    output = directory / f"page_{page_number:03d}.png"
    prompt_path = directory / f"page_{page_number:03d}.prompt.txt"
    trace_path = directory / f"page_{page_number:03d}.trace.json"
    receipt_path = directory / f"page_{page_number:03d}.json"
    blocked_path = directory / f"page_{page_number:03d}.blocked.json"
    request = build_initial_request(bundle, style, output, project=root)
    required_assets = required_reference_assets(bundle)
    required_reference_hashes = {
        item["sha256"] for item in _reference_inputs(request)
        if item["presence_role"] == "required_presence"
    }
    registry_hashes = {item["sha256"] for item in required_assets}
    if required_reference_hashes != registry_hashes:
        raise ValueError(
            "Image2 required references differ from the canonical authentic asset registry"
        )
    slot_plan = build_asset_slot_plan(required_assets)
    if slot_plan:
        slot_instruction = (
            "\nAUTHENTIC_ASSET_SLOT_PLAN: "
            + json.dumps(slot_plan, ensure_ascii=False, sort_keys=True)
            + "\nSLOT_PLAN_POLICY: The entire authentic-material rail is owned by deterministic local "
              "composition. Do not draw, imitate, duplicate, label, or place any supplied reference "
              "photo or enterprise logo anywhere in the model image. Keep body text outside the rail. "
              "Design the remaining page with balanced hierarchy and spacing; the local compositor will "
              "erase the rail and place each exact source asset once.\n"
        )
        prompt = request.prompt + slot_instruction
        request = replace(
            request,
            prompt=prompt,
            authority_prompt_sha256=hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
        )
    page_title = _prompt_json_field(request.prompt, "FIXED_PAGE_TITLE_INPUT_ONLY_NOT_RENDERED")
    references = _reference_inputs(request)
    semantic_inputs = {
        "page_number": page_number,
        "prompt_sha256": request.authority_prompt_sha256,
        "material_bundle_sha256": request.material_bundle_sha256,
        "style_execution_sha256": request.style_execution_sha256,
        "operation": request.operation,
        "image_model": "gpt-image-2",
        "size": request.size,
        "quality": request.quality,
        "reference_sha256": [item["sha256"] for item in references],
        "required_asset_slot_plan": slot_plan,
        "required_asset_slot_plan_id": slot_plan_identity(slot_plan),
        "fixed_logo_reference": _logo_reference_identity(logo_reference),
        "acceptance_policy": _ACCEPTANCE_POLICY,
        "semantic_qa_role": "image2-design-qa",
        "semantic_repairs_max": 1,
    }
    current_slot_plan_id = semantic_inputs["required_asset_slot_plan_id"]
    prior_receipt = None
    if receipt_path.is_file():
        try:
            candidate_receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            candidate_output = root / str(candidate_receipt.get("output", ""))
            if (
                candidate_receipt.get("acceptance", {}).get("outcome") == "pass"
                and candidate_output.resolve() == output.resolve()
                and output.is_file()
                and candidate_receipt.get("artifact_id") == "sha256:" + _sha256_file(output)
                and _verify_image(output) == (1904, 896)
            ):
                prior_receipt = candidate_receipt
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            prior_receipt = None
    if prior_receipt is None and blocked_path.is_file() and output.is_file():
        try:
            blocked_receipt = json.loads(blocked_path.read_text(encoding="utf-8"))
            if (
                blocked_receipt.get("outcome") == "blocked"
                and blocked_receipt.get("raw_image2", {}).get("path")
                    == output.relative_to(root).as_posix()
                and blocked_receipt.get("raw_image2", {}).get("artifact_id")
                    == "sha256:" + _sha256_file(output)
                and _verify_image(output) == (1904, 896)
            ):
                # A policy or slot-plan upgrade may make the same expensive
                # Image2 pixels acceptable after deterministic recomposition.
                # Reuse the raw candidate; the new semantic identity still
                # reruns preflight and QA and can use one targeted repair.
                prior_receipt = {
                    "backend_calls": int(blocked_receipt.get("backend_calls", 0)),
                    "trace": trace_path.relative_to(root).as_posix() if trace_path.is_file() else None,
                    "blocked_candidate_reuse": True,
                    "generation_slot_plan_id": blocked_receipt.get("generation_slot_plan_id"),
                }
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            prior_receipt = None
    worker = f"v5-image2-design:{page_number}"
    ledger = RequestLedger(root)
    claim = ledger.claim("image2_design", semantic_inputs, worker_id=worker)
    if claim["decision"] == "busy":
        raise ValueError("equivalent V5 Image2 design request is already running")
    if claim["decision"] == "reuse":
        if claim.get("outcome") == "negative":
            raise DesignAcceptanceBlocked(
                "cached terminal Image2 design acceptance failure: " + str(claim.get("reason"))
            )
        cached = claim["result"]
        cached_path = root / cached["output"]
        if cached_path.is_file() and receipt_path.is_file():
            return json.loads(receipt_path.read_text(encoding="utf-8"))
        raise ValueError("cached V5 Image2 design artifacts are missing")

    # A new semantic identity supersedes any fixed-path receipt from an older
    # accepted design. Never leave stale acceptance evidence beside a blocked run.
    receipt_path.unlink(missing_ok=True)
    blocked_path.unlink(missing_ok=True)
    prompt_path.write_text(request.prompt, encoding="utf-8")
    command = [
        sys.executable, str(IMAGE_CLI), request.operation,
        "--prompt-file", str(prompt_path), "--out", str(output),
        "--trace-out", str(trace_path), "--model", "gpt-image-2",
        "--size", request.size, "--quality", request.quality,
        "--allow-off-ratio-for-downstream-repair",
    ]
    if not slot_plan:
        for item in references:
            command.extend([
                "--image", item["path"],
                "--image-role", f'{item["presence_role"]}:{item["source_role"]}:{item["asset_id"]}',
            ])
    backend_calls = 0
    qa_invocations: list[dict[str, Any]] = []
    ratio_repair_used = False
    initial_size: tuple[int, int] | None = None
    semantic_repairs_used = 0
    terminal_negative = False
    generation_reused = prior_receipt is not None
    generation_slot_plan_id = (
        prior_receipt.get("generation_slot_plan_id")
        if generation_reused else current_slot_plan_id
    )
    try:
        if generation_reused:
            initial_size = (1904, 896)
            prior_trace = prior_receipt.get("trace")
            if isinstance(prior_trace, str) and (root / prior_trace).is_file():
                trace_path = root / prior_trace
        else:
            _run_image(command, timeout=timeout, label="Image2 design")
            backend_calls += 1
            initial_size = _verify_image(output)
        ratio_repair_used = initial_size != (1904, 896)
        if ratio_repair_used:
            repair_output = directory / f"page_{page_number:03d}.ratio-repair.png"
            repair_prompt = directory / f"page_{page_number:03d}.ratio-repair.prompt.txt"
            repair_trace = directory / f"page_{page_number:03d}.ratio-repair.trace.json"
            repair_prompt.write_text(
                request.prompt
                + "\nRATIO_REPAIR_ONLY: Recompose the same approved design into an exact 1904x896 "
                  "17:8 body canvas. Preserve all authorized content, hierarchy, palette, and visual "
                  "quality. Do not stretch, crop, letterbox, or add fixed title/logo/footer/page number; "
                  "re-layout the design naturally to use the wide canvas.",
                encoding="utf-8",
            )
            _run_image([
                sys.executable, str(IMAGE_CLI), "edit",
                "--prompt-file", str(repair_prompt), "--out", str(repair_output),
                "--trace-out", str(repair_trace), "--model", "gpt-image-2",
                "--size", request.size, "--quality", request.quality,
                "--image", str(output), "--image-role", "ratio-repair-source",
            ], timeout=timeout, label="Image2 ratio repair")
            backend_calls += 1
            _verify_image(repair_output)
            os.replace(repair_output, output)
            trace_path = repair_trace
            generation_slot_plan_id = current_slot_plan_id

        acceptance_image = output
        acceptance_composed = None
        acceptance_composition = None
        if slot_plan:
            acceptance_composed = directory / f"page_{page_number:03d}.acceptance-composed.png"
            acceptance_composition = compose_candidate_body(
                root, page_number, output, acceptance_composed,
            )
            acceptance_image = acceptance_composed
        deterministic_image2_preflight(acceptance_image, expected_size=(1904, 896))

        qa = _review_with_ledger(
            ledger, root=root, image=acceptance_image, logo_reference=logo_reference,
            page_number=page_number, page_title=page_title, bundle=bundle,
            references=references, authority_sha256=request.material_bundle_sha256,
            timeout=timeout,
        )
        qa_invocations.append(_qa_invocation(qa, attempt=1))
        accepted = bool(qa["accepted"])
        failed_candidate = acceptance_image
        if not accepted:
            semantic_repairs_used = 1
            semantic_output = directory / f"page_{page_number:03d}.semantic-repair.png"
            semantic_prompt = directory / f"page_{page_number:03d}.semantic-repair.prompt.txt"
            semantic_trace = directory / f"page_{page_number:03d}.semantic-repair.trace.json"
            semantic_prompt.write_text(
                _semantic_repair_text(request.prompt, qa["issues"]), encoding="utf-8",
            )
            repair_command = [
                sys.executable, str(IMAGE_CLI), "edit",
                "--prompt-file", str(semantic_prompt), "--out", str(semantic_output),
                "--trace-out", str(semantic_trace), "--model", "gpt-image-2",
                "--size", request.size, "--quality", request.quality,
                "--image", str(output), "--image-role", "semantic-repair-source",
            ]
            if not slot_plan:
                for item in references:
                    repair_command.extend([
                        "--image", item["path"],
                        "--image-role", f'{item["presence_role"]}:{item["source_role"]}:{item["asset_id"]}',
                    ])
            _run_image(repair_command, timeout=timeout, label="Image2 semantic repair")
            backend_calls += 1
            _verify_image(semantic_output)
            repaired_acceptance = semantic_output
            repaired_composed = None
            if slot_plan:
                repaired_composed = directory / f"page_{page_number:03d}.semantic-repair.composed.png"
                repaired_composition = compose_candidate_body(
                    root, page_number, semantic_output, repaired_composed,
                )
                repaired_acceptance = repaired_composed
            deterministic_image2_preflight(repaired_acceptance, expected_size=(1904, 896))
            repaired_qa = _review_with_ledger(
                ledger, root=root, image=repaired_acceptance, logo_reference=logo_reference,
                page_number=page_number, page_title=page_title, bundle=bundle,
                references=references, authority_sha256=request.material_bundle_sha256,
                timeout=timeout,
            )
            qa_invocations.append(_qa_invocation(repaired_qa, attempt=2))
            accepted = bool(repaired_qa["accepted"])
            failed_candidate = repaired_acceptance
            if accepted:
                os.replace(semantic_output, output)
                generation_slot_plan_id = current_slot_plan_id
                if repaired_composed is not None and acceptance_composed is not None:
                    os.replace(repaired_composed, acceptance_composed)
                    acceptance_composition = repaired_composition
                trace_path = semantic_trace

        if not accepted:
            blocked = {
                "artifact_version": "v5-image2-design-blocked-v1",
                "page_number": page_number,
                "outcome": "blocked",
                "reason": "semantic acceptance failed after the one allowed issue-targeted repair",
                "acceptance_policy": _ACCEPTANCE_POLICY,
                "semantic_repairs_used": semantic_repairs_used,
                "semantic_qa_calls": len(qa_invocations),
                "backend_calls": backend_calls,
                "generation_slot_plan_id": generation_slot_plan_id,
                "raw_image2": {
                    "path": output.relative_to(root).as_posix(),
                    "artifact_id": "sha256:" + _sha256_file(output),
                },
                "fixed_logo_reference": _logo_reference_identity(logo_reference),
                "candidate": failed_candidate.relative_to(root).as_posix(),
                "candidate_sha256": _sha256_file(failed_candidate),
                "invocations": qa_invocations,
            }
            _write_json(blocked_path, blocked)
            ledger.complete_negative(
                claim["request_key"], worker_id=worker, reason=blocked["reason"],
                result={
                    "report": blocked_path.relative_to(root).as_posix(),
                    "model": "gpt-image-2", "auth_mode": "codex_oauth",
                    "quality": request.quality, "backend_calls": backend_calls,
                    "model_invocations": [{
                        "purpose": "image2_design", "model": "gpt-image-2",
                        "auth_mode": "codex_oauth", "strength": request.quality,
                        "provider_backend_calls": backend_calls,
                    }],
                },
            )
            terminal_negative = True
            raise DesignAcceptanceBlocked(blocked["reason"])

        record = ContentCatalog(root).record_file(
            f"page-{page_number:03d}-design-v5", output,
            boundary="after_external_output",
        )
        receipt = {
            "artifact_version": "v5-image2-design-v3",
            "page_number": page_number,
            "model": "gpt-image-2",
            "auth_mode": "codex_oauth",
            "operation": request.operation,
            "size": [1904, 896],
            "quality": request.quality,
            "backend_calls": backend_calls,
            "generation_reused": generation_reused,
            "generation_slot_plan_id": generation_slot_plan_id,
            "prior_generation_backend_calls": (
                int(prior_receipt.get("backend_calls", 0)) if prior_receipt is not None else 0
            ),
            "ratio_repair_used": ratio_repair_used,
            "initial_size": list(initial_size),
            "output": output.relative_to(root).as_posix(),
            "artifact_id": record["artifact_id"],
            "prompt_sha256": hashlib.sha256(request.prompt.encode("utf-8")).hexdigest(),
            "trace": trace_path.relative_to(root).as_posix(),
            "acceptance": {
                "policy": _ACCEPTANCE_POLICY,
                "outcome": "pass",
                "deterministic_preflight": "passed_before_each_semantic_review",
                "reviewed_visual_authority": (
                    "accepted_composed_body" if slot_plan else "accepted_image2_body"
                ),
                "reviewed_composed_body": (
                    {
                        "path": acceptance_composed.relative_to(root).as_posix(),
                        "artifact_id": "sha256:" + _sha256_file(acceptance_composed),
                        "slot_plan": acceptance_composition["slot_plan"],
                        "slot_plan_identity": acceptance_composition["slot_plan_identity"],
                        "authentic_placements": acceptance_composition["authentic_placements"],
                    }
                    if acceptance_composed is not None else None
                ),
                "semantic_repairs_used": semantic_repairs_used,
                "semantic_qa_calls": len(qa_invocations),
                "invocations": qa_invocations,
                "fixed_logo_reference": _logo_reference_identity(logo_reference),
            },
        }
        _write_json(receipt_path, receipt)
        result = {
            "output": receipt["output"], "artifact_id": record["artifact_id"],
            "model": "gpt-image-2", "auth_mode": "codex_oauth",
            "quality": request.quality, "backend_calls": backend_calls,
            "ratio_repair_used": ratio_repair_used,
            "semantic_repairs_used": semantic_repairs_used,
            "acceptance_outcome": "pass",
            "model_invocations": [{
                "purpose": "image2_design", "model": "gpt-image-2",
                "auth_mode": "codex_oauth", "strength": request.quality,
                "provider_backend_calls": backend_calls,
            }],
        }
        ledger.complete_success(claim["request_key"], worker_id=worker, result=result)
        blocked_path.unlink(missing_ok=True)
        return receipt
    except DesignAcceptanceBlocked:
        if not terminal_negative:
            ledger.fail_retryable(
                claim["request_key"], worker_id=worker,
                reason="unexpected non-terminal design acceptance block",
            )
        raise
    except Exception as exc:
        ledger.fail_retryable(claim["request_key"], worker_id=worker, reason=str(exc))
        raise
