#!/usr/bin/env python3
"""Build a minimal current project and exercise installed editppt record/finalize."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from docx import Document


def _run(command: list[str], *, env: dict[str, str]) -> dict:
    completed = subprocess.run(command, capture_output=True, text=True, env=env, check=False)
    if completed.returncode:
        raise RuntimeError(
            f"portable command failed ({completed.returncode}): {' '.join(command)}\n"
            + completed.stdout
            + completed.stderr
        )
    return json.loads(completed.stdout)


def smoke(editppt: Path, output: Path) -> dict:
    if output.exists():
        shutil.rmtree(output)
    output.mkdir(parents=True)
    project = output / "project"
    page_title = "便携安装完整链路"
    body_text = "正文重建和固定框架均可执行。"
    source = output / "source.docx"
    document = Document()
    document.add_paragraph("第 1 页")
    document.add_paragraph(page_title)
    document.add_paragraph(body_text)
    document.save(source)
    logo = output / "logo.svg"
    logo.write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 120 48">'
        '<rect width="120" height="48" fill="#22577A"/></svg>',
        encoding="utf-8",
    )

    from confirm_ui.server import _wait, create_app
    from editppt.runtime.fixed_region_runtime import CONTENT_BOX, SLIDE
    from prepare_run import prepare
    import workflow_state

    prepared = prepare(source, project, logo)
    if prepared.get("page_count") != 1:
        raise RuntimeError("portable prepare did not create exactly one V4 page")
    client = create_app(project).test_client()
    recommendations = client.get("/api/recommendations").get_json()
    selected = recommendations["design_directions"]["selected"]
    candidate = recommendations["design_directions"]["candidates"][selected]
    confirmation = {
        "stage": "final",
        "direction": selected,
        "template_selection": candidate["template_selection"],
        "canvas": "ppt169",
        **{
            key: candidate[key]
            for key in (
                "visual_style", "color", "icons", "typography", "image_rendering",
                "style_axes", "layout_preferences", "information_density",
                "background_system", "image_role", "evidence_strength",
                "composition_tendency", "brand_device",
            )
        },
        "regional_style": {"enabled": False},
        "production_profile": "balanced",
        "additional_requirements": "保持Word主叙事和逐页证据边界",
    }
    response = client.post("/api/confirm", json=confirmation)
    if response.status_code != 200 or _wait(project, "final", 5) != 0:
        raise RuntimeError(f"portable V4 confirmation failed: {response.get_json()}")

    workflow_env = dict(os.environ)
    workflow_python = Path(sys.executable)
    state = workflow_state.load(project)
    if state.get("workflow_contract_version") != "word-ppt-workflow-v4" or state.get("style_confirmation", {}).get("status") != "confirmed":
        raise RuntimeError("portable prepare/confirmation did not reach the current V4 boundary")

    page_dir = output / "editable-page"
    page_dir.mkdir()
    manifest = {
        "workflow_contract_version": "fixed-canvas-cm-v2",
        "reconstruction_contract_version": "editable-image-v3",
        "slide": dict(SLIDE),
        "content_box": dict(CONTENT_BOX),
        "source": {"width_px": 1700, "height_px": 800},
        "text_boxes": [{"object_id": "word-p1", "name": "body-paragraph-1", "text": body_text, "box_px": [80, 60, 1200, 100]}],
        "tables": [],
        "shapes": [{"object_id": "decor-1", "name": "decorative-panel", "type": "rect", "box_px": [40, 30, 1500, 650], "fill": "#F4F4F4"}],
        "images": [],
        "visual_inventory": [],
        "background_strategy": "native slide background plus editable decorative panel",
        "quality_checks": {
            "font_size_calibrated": True,
            "visual_inventory_matched": True,
            "background_strategy_checked": True,
            "shape_corner_geometry_checked": True,
        },
    }
    (page_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    built = subprocess.run([str(editppt), "page", "build", str(page_dir)], capture_output=True, text=True, env=workflow_env, check=False)
    if built.returncode:
        raise RuntimeError(built.stdout + built.stderr)
    validated = subprocess.run([str(editppt), "page", "validate", str(page_dir)], capture_output=True, text=True, env=workflow_env, check=False)
    if validated.returncode:
        raise RuntimeError(validated.stdout + validated.stderr)
    if not (page_dir / "page.pptx").is_file() or not (page_dir / "preview.png").is_file():
        raise RuntimeError("portable V4 object build did not create PPTX and preview")
    return {"workflow": "word-ppt-workflow-v4", "editppt": "v4-build-validate-ok"}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--editppt", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = smoke(args.editppt.resolve(), args.output.resolve())
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
