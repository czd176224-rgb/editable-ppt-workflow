from __future__ import annotations

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

from workflow_v6_image import ImageRequest  # noqa: E402
import workflow_v6_qa  # noqa: E402
from workflow_v6_qa import (  # noqa: E402
    actionable_retry_feedback,
    mechanical_review,
    semantic_review,
)


def _valid_artifacts(tmp_path: Path) -> tuple[ImageRequest, Path, dict]:
    output = tmp_path / "candidate.png"
    Image.new("RGB", (1904, 896), "white").save(output)
    prompt = "approved prompt"
    request = ImageRequest(
        operation="generate",
        quality="medium",
        prompt=prompt,
        input_images=(),
        image_roles=(),
        input_sha256s=(),
    )
    digest = hashlib.sha256(output.read_bytes()).hexdigest()
    trace = {
        "operation": "generate",
        "model": "gpt-image-2",
        "quality": "medium",
        "size": "1904x896",
        "input_images": [],
        "outputs": [{
            "path": str(output.resolve()),
            "sha256": digest,
            "mime_type": "image/png",
        }],
    }
    trace_path = output.with_suffix(".trace.json")
    trace_path.write_text(json.dumps(trace), encoding="utf-8")
    inputs = {
        "trace_path": trace_path,
        "visual_contract": {"visual_style": "minimal editorial"},
    }
    return request, output, inputs


def test_mechanical_review_accepts_a_bound_valid_candidate(tmp_path: Path):
    request, output, receipt_inputs = _valid_artifacts(tmp_path)

    result = mechanical_review(
        request=request, output=output, receipt_inputs=receipt_inputs,
    )

    assert result["accepted"] is True
    assert result["issues"] == []


@pytest.mark.parametrize(
    ("damage", "expected_code"),
    [
        ("missing", "output_missing"),
        ("undecodable", "output_undecodable"),
        ("wrong_size", "output_wrong_size"),
        ("wrong_mime", "output_wrong_mime"),
        ("operation", "operation_input_mismatch"),
        ("input_digest", "input_digest_mismatch"),
        ("trace_output", "output_trace_mismatch"),
        ("prompt_limit", "prompt_over_limit"),
        ("empty_style", "visual_style_empty"),
    ],
)
def test_mechanical_review_rejects_every_local_contract_break(
    tmp_path: Path, damage: str, expected_code: str,
):
    request, output, receipt_inputs = _valid_artifacts(tmp_path)
    trace_path = Path(receipt_inputs["trace_path"])
    trace = json.loads(trace_path.read_text(encoding="utf-8"))

    if damage == "missing":
        output.unlink()
    elif damage == "undecodable":
        output.write_bytes(b"not an image")
    elif damage == "wrong_size":
        Image.new("RGB", (1024, 1024), "white").save(output)
    elif damage == "wrong_mime":
        Image.new("RGB", (1904, 896), "white").save(output, format="JPEG")
    elif damage == "operation":
        request = ImageRequest(
            operation="edit", quality="medium", prompt=request.prompt,
            input_images=(), image_roles=(), input_sha256s=(),
        )
    elif damage == "input_digest":
        reference = tmp_path / "reference.png"
        Image.new("RGB", (8, 8), "red").save(reference)
        request = ImageRequest(
            operation="edit", quality="medium", prompt=request.prompt,
            input_images=(reference,), image_roles=("evidence",),
            input_sha256s=("0" * 64,),
        )
        trace["operation"] = "edit"
        trace["input_images"] = [{
            "path": str(reference), "role": "evidence", "sha256": "0" * 64,
        }]
        trace_path.write_text(json.dumps(trace), encoding="utf-8")
    elif damage == "trace_output":
        trace["outputs"][0]["sha256"] = "0" * 64
        trace_path.write_text(json.dumps(trace), encoding="utf-8")
    elif damage == "prompt_limit":
        request = ImageRequest(
            operation="generate", quality="medium", prompt="x" * 32_001,
            input_images=(), image_roles=(), input_sha256s=(),
        )
    elif damage == "empty_style":
        receipt_inputs["visual_contract"] = {"visual_style": "   "}

    result = mechanical_review(
        request=request, output=output, receipt_inputs=receipt_inputs,
    )

    assert result["accepted"] is False
    assert expected_code in {issue["code"] for issue in result["issues"]}


def test_semantic_review_uses_only_frozen_contract_and_declares_exclusions(
    tmp_path: Path, monkeypatch,
):
    image = tmp_path / "project" / "04_v6" / "images" / "candidate.png"
    image.parent.mkdir(parents=True)
    Image.new("RGB", (1904, 896), "white").save(image)
    observed = {}

    class Result:
        value = {
            "checks": {
                code: {"result": "pass", "detail": "compliant", "correction": ""}
                for code in workflow_v6_qa.SEMANTIC_CHECKS
            },
            "issues": [],
        }

    def invoke(project, **kwargs):
        observed.update(project=project, **kwargs)
        return Result()

    monkeypatch.setattr(workflow_v6_qa, "invoke_structured", invoke)
    result = semantic_review(
        image=image,
        confirmed_page={
            "effective_body": "Approved body",
            "image_requirements": [{"requirement": "show the confirmed meeting photo"}],
            "local_path": "D:/secret/reference.png",
            "sha256": "deadbeef" * 8,
        },
        visual_contract={
            "visual_style": "minimal editorial",
            "receipt": "local-receipt-sentinel",
        },
        reference_roles=["meeting photo evidence", "logo high-fidelity best effort"],
    )

    prompt = observed["prompt"]
    assert result["accepted"] is True
    assert observed["images"] == [image]
    assert "Approved body" in prompt
    assert "meeting photo evidence" in prompt
    assert "pixel identity" in prompt
    assert "post-reconstruction" in prompt
    assert "naturally present" in prompt
    assert "fixed main title" in prompt
    assert str(image) not in prompt
    assert "D:/secret/reference.png" not in prompt
    assert "deadbeef" not in prompt
    assert "local-receipt-sentinel" not in prompt


def test_actionable_retry_feedback_requires_new_structured_corrections():
    previous = {
        "checks": {"fixed_layers_absent": {
            "result": "fail", "detail": "Main title is present",
            "correction": "Remove the generated main title from the body.",
        }},
        "issues": [],
    }
    current = {
        "checks": {
            "fixed_layers_absent": {
                "result": "fail", "detail": "Main title is present",
                "correction": "Remove the generated main title from the body.",
            },
            "global_style_followed": {
                "result": "fail", "detail": "Colors ignore the approved palette",
                "correction": "Use the approved navy and gold palette.",
            },
        },
        "issues": [{"code": "vague", "correction": "   "}],
    }

    assert actionable_retry_feedback(current, previous) == [
        "Use the approved navy and gold palette."
    ]
    assert actionable_retry_feedback(previous, previous) == []
    assert actionable_retry_feedback({"issues": ["bad"]}, None) == []


def test_retry_feedback_rejects_prose_details_raw_issues_and_unknown_checks():
    result = {
        "checks": {
            "global_style_followed": {
                "result": "fail",
                "detail": "Increase contrast and use the approved palette.",
            },
            "unknown_reviewer_opinion": {
                "result": "fail",
                "detail": "Known-looking prose",
                "correction": "Remove all decorative elements.",
            },
        },
        "issues": [
            "Increase contrast between the text and panel.",
            {"code": "free_form", "correction": "Move the chart to the left."},
        ],
    }

    assert actionable_retry_feedback(result, None) == []


def test_review_candidate_honors_a_lower_caller_timeout(tmp_path: Path, monkeypatch):
    image = tmp_path / "project" / "04_v6" / "images" / "candidate.png"
    image.parent.mkdir(parents=True)
    Image.new("RGB", (1904, 896), "white").save(image)
    observed = {}

    class Result:
        value = {
            "checks": {
                code: {"result": "pass", "detail": "compliant", "correction": ""}
                for code in workflow_v6_qa.SEMANTIC_CHECKS
            },
            "issues": [],
        }

    def invoke(project, **kwargs):
        observed.update(project=project, **kwargs)
        return Result()

    monkeypatch.setattr(workflow_v6_qa, "invoke_structured", invoke)
    workflow_v6_qa.review_candidate(
        image.parents[2],
        image=image,
        effective_page={"effective_body": "Approved body"},
        style_contract={"visual_style": "minimal"},
        fixed_logo_name="fixed-logo",
        timeout=7.5,
    )

    assert observed["timeout"] == 7.5


@pytest.mark.parametrize(
    "correction",
    [
        "Preserve the confirmed logo proportions and keep it recognizable.",
        "Keep the confirmed meeting photo recognizable in the left panel.",
        "Maintain the approved navy and gold visual hierarchy.",
        "Restore the confirmed screenshot content without severe distortion.",
        "Remove the generated main title from the body region.",
        "Avoid fabricating an unconfirmed institution or event identity.",
        "Ensure the confirmed reference remains recognizable after fusion.",
        "Increase contrast between the body text and background panels.",
        "Reduce decorative clutter around the confirmed evidence image.",
        "Improve alignment between the chart and its explanatory text.",
        "Align the lower evidence panel with the approved grid.",
        "Use the approved minimal editorial color palette.",
        "Replace the fabricated product image with abstract geometry.",
        "Correct the severe distortion in the confirmed screenshot.",
        "保留已确认徽标的原始比例并确保清晰可识别。",
        "保持已确认会议照片在左侧区域中清晰可识别。",
        "恢复已确认截图内容并避免严重变形。",
        "移除正文区域中生成的固定页面主标题。",
        "避免虚构未经确认的机构、事件或产品身份。",
        "确保已确认参考图片融合后仍然清晰可识别。",
        "提高正文文字与背景面板之间的对比度。",
        "减少已确认真实材料周围不必要的装饰元素。",
        "改善图表与说明文字之间的对齐关系。",
        "使用用户确认的简洁编辑风格配色。",
        "替换未经确认的真实产品图片为抽象图形。",
        "修正已确认截图中存在的严重变形。",
    ],
)
def test_retry_feedback_accepts_deterministic_imperative_corrections(correction: str):
    current = {
        "checks": {
            "confirmed_content_and_requirements": {
                "result": "fail",
                "detail": "contract mismatch",
                "correction": correction,
            },
        },
        "issues": [],
    }

    assert actionable_retry_feedback(current, None) == [correction]


@pytest.mark.parametrize(
    "correction",
    [
        "The logo is still wrong.",
        "logo",
        "logo overlap contrast",
        "Fix logo",
        "Remove logo",
        "Ensure logo?",
        "Increase contrast?",
        "Is the logo recognizable?",
        "徽标仍然不正确。",
        "徽标 重叠 对比度",
        "修复徽标",
        "移除徽标",
        "确保徽标？",
    ],
)
def test_retry_feedback_rejects_status_nouns_short_commands_and_questions(correction: str):
    current = {
        "checks": {
            "confirmed_content_and_requirements": {
                "result": "fail",
                "detail": "contract mismatch",
                "correction": correction,
            },
        },
        "issues": [],
    }

    assert actionable_retry_feedback(current, None) == []
