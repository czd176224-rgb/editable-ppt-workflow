"""Build minimal, immutable requests for one page image at a time."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

from style_contract import canonical_json_bytes


DEFAULT_MODEL = "gpt-image-2"
DEFAULT_SIZE = "1792x1008"
DEFAULT_QUALITY = "high"
FIDELITY_BOUNDARY = "Render only the supplied current-page text; do not invent facts or text."
STRUCTURAL_SCOPES = frozenset({"structural", "logic", "overall_style"})
LEGAL_CANVAS_SIZES = frozenset({"1792x1008", "1536x1152"})
LEGAL_QUALITIES = frozenset({"auto", "low", "medium", "high"})


def _freeze(value: Any) -> Any:
    if isinstance(value, dict):
        return MappingProxyType({str(key): _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    return value


def _thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value


def _style_execution(style: dict) -> tuple[Mapping[str, Any], str]:
    if not isinstance(style, dict):
        raise ValueError("style must be the frozen style contract result")
    execution = style.get("execution", style.get("style_execution"))
    digest = style.get("sha256", style.get("style_execution_sha256"))
    if not isinstance(execution, dict) or not isinstance(digest, str) or not digest.strip():
        raise ValueError("style must include execution/style_execution and sha256/style_execution_sha256")
    expected_digest = hashlib.sha256(canonical_json_bytes(execution)).hexdigest()
    if digest != expected_digest:
        raise ValueError("style execution SHA-256 mismatch")
    return _freeze(execution), digest


def _generation_settings(execution: Mapping[str, Any]) -> tuple[str, str]:
    profile = execution.get("canvas_profile")
    if not isinstance(profile, Mapping):
        raise ValueError("style execution must include a canvas profile")
    size = profile.get("image_size")
    if size not in LEGAL_CANVAS_SIZES or profile.get("fit") != "contain" or profile.get("allow_crop") is not False:
        raise ValueError("style execution canvas profile is not a legal no-crop image contract")
    quality = execution.get("image_quality")
    if quality not in LEGAL_QUALITIES:
        raise ValueError("style execution image quality is invalid")
    return str(size), str(quality)


def _normalize_issue(value: Any) -> str | None:
    if isinstance(value, str):
        text = " ".join(value.split())
        return text[:280] if text else None
    if isinstance(value, dict):
        for field in ("message", "description", "issue", "text"):
            text = _normalize_issue(value.get(field))
            if text:
                return text
    return None


def _issues(qa: dict) -> tuple[str, ...]:
    if not isinstance(qa, dict):
        raise ValueError("qa must be an object with concrete repair issues")
    raw = qa.get("issues", qa.get("issue"))
    values = raw if isinstance(raw, list) else [raw]
    normalized = tuple(issue for value in values if (issue := _normalize_issue(value)))
    if not normalized:
        raise ValueError("qa must include at least one concrete repair issue")
    return normalized


def _scope_is_structural(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    normalized = value.strip().lower().replace("-", "_").replace(" ", "_")
    return normalized in STRUCTURAL_SCOPES


def _requires_fresh_generation(qa: dict) -> bool:
    for field in ("repair_scope", "scope", "issue_type", "category"):
        if _scope_is_structural(qa.get(field)):
            return True
    raw = qa.get("issues", qa.get("issue"))
    values = raw if isinstance(raw, list) else [raw]
    for issue in values:
        if not isinstance(issue, dict):
            continue
        if issue.get("structural") is True:
            return True
        if any(_scope_is_structural(issue.get(field)) for field in ("scope", "issue_type", "category")):
            return True
    return False


@dataclass(frozen=True)
class GenerationRequest:
    """A serializable request with only the inputs authorized for this page."""

    operation: str
    page_text: str
    style_execution: Mapping[str, Any]
    style_execution_sha256: str
    output: Path | None
    model: str = DEFAULT_MODEL
    size: str = DEFAULT_SIZE
    quality: str = DEFAULT_QUALITY
    fidelity_boundary: str = FIDELITY_BOUNDARY
    prior_image: Path | None = None
    repair_issues: tuple[str, ...] = ()

    @property
    def endpoint(self) -> str:
        return "images/edits" if self.operation == "edit" else "images/generations"

    @property
    def payload(self) -> dict[str, Any]:
        """Return a detached payload suitable for logging or CLI translation."""
        payload: dict[str, Any] = {
            "operation": self.operation,
            "endpoint": self.endpoint,
            "page_text": self.page_text,
            "style_execution": _thaw(self.style_execution),
            "style_execution_sha256": self.style_execution_sha256,
            **({"output": str(self.output)} if self.output is not None else {}),
            "model": self.model,
            "size": self.size,
            "quality": self.quality,
            "fidelity_boundary": self.fidelity_boundary,
        }
        if self.prior_image is not None:
            payload["prior_image"] = str(self.prior_image)
        if self.repair_issues:
            payload["repair_issues"] = list(self.repair_issues)
        return payload


def _request(
    operation: str,
    page_text: str,
    style: dict,
    output: Path | None = None,
    prior_image: Path | None = None,
    repair_issues: tuple[str, ...] = (),
) -> GenerationRequest:
    if operation not in {"generate", "edit"}:
        raise ValueError("operation must be generate or edit")
    if not isinstance(page_text, str) or not page_text:
        raise ValueError("page_text must be a non-empty string")
    execution, digest = _style_execution(style)
    size, quality = _generation_settings(execution)
    return GenerationRequest(
        operation=operation,
        page_text=page_text,
        style_execution=execution,
        style_execution_sha256=digest,
        output=Path(output).resolve() if output is not None else None,
        prior_image=Path(prior_image).resolve() if prior_image is not None else None,
        repair_issues=repair_issues,
        size=size,
        quality=quality,
    )


def build_initial_request(page_text: str, style: dict, output: Path) -> GenerationRequest:
    """Create the text-only initial generation request for one current page."""
    return _request("generate", page_text, style, output=output)


def build_repair_request(page_text: str, style: dict, prior_image: Path, qa: dict) -> GenerationRequest:
    """Use edit only for local repairs; regenerate structure, logic, or overall style."""
    issues = _issues(qa)
    if _requires_fresh_generation(qa):
        return _request("generate", page_text, style, repair_issues=issues)
    return _request("edit", page_text, style, prior_image=prior_image, repair_issues=issues)
