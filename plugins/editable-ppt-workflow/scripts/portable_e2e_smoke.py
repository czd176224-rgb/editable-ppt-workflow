#!/usr/bin/env python3
"""Build a minimal current project and exercise installed editppt record/finalize."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from PIL import Image
from pptx import Presentation
from pptx.util import Inches


def _canonical(value: dict) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _write(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


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
    project.mkdir()
    source_text = "便携安装完整链路"
    contract = {
        "schema_version": "2.0",
        "page_number": 1,
        "source_text": source_text,
        "source_hash": hashlib.sha256(source_text.encode()).hexdigest(),
    }
    _write(project / "01_page_contracts" / "page_001.json", contract)
    style = {
        "schema_version": "1.0",
        "canvas": "ppt169",
        "canvas_profile": {
            "image_size": "1792x1008",
            "slide_width_inches": 13.333333,
            "slide_height_inches": 7.5,
            "fit": "contain",
            "allow_crop": False,
        },
        "image_quality": "auto",
        "generation_mode": "continuous",
        "max_concurrency": 1,
        "automatic_repair_budget": 1,
    }
    style_bytes = _canonical(style)
    style_path = project / "02_style" / "style_execution.json"
    style_path.parent.mkdir()
    style_path.write_bytes(style_bytes)
    _write(
        project / "workflow_run.json",
        {
            "schema_version": "1.0",
            "workflow_contract_version": "word-only-v1",
            "project_name": "portable-e2e",
            "created_at": "2026-07-27T00:00:00Z",
            "word_source": {},
            "pagination": {"page_count": 1, "locked_page_order": [1]},
            "style_confirmation": {
                "status": "confirmed",
                "confirmed_at": "2026-07-27T00:00:00Z",
                "execution_file": "02_style/style_execution.json",
                "execution_sha256": hashlib.sha256(style_bytes).hexdigest(),
            },
            "jobs": [
                {
                    "slide_id": "slide_001",
                    "page_number": 1,
                    "status": "accepted",
                    "contract_file": "01_page_contracts/page_001.json",
                    "expected_output": "06_images/generated/page_001.png",
                    "generation": {"image": "06_images/generated/page_001.png"},
                    "qa_result": {"status": "pass", "repair_scope": "none", "issues": []},
                }
            ],
            "final_pptx": None,
        },
    )
    generated = project / "06_images" / "generated" / "page_001.png"
    generated.parent.mkdir(parents=True)
    Image.new("RGB", (1792, 1008), "white").save(generated)

    presentation = Presentation()
    presentation.slide_width = Inches(13.333333)
    presentation.slide_height = Inches(7.5)
    slide = presentation.slides.add_slide(presentation.slide_layouts[6])
    slide.shapes.add_textbox(Inches(1), Inches(1), Inches(5), Inches(1)).text = source_text
    pptx = project / "07_editable" / "page_001.pptx"
    pptx.parent.mkdir()
    presentation.save(pptx)

    fake = output / "renderer"
    fake.mkdir()
    (fake / "render_pptx.py").write_text(
        "from pathlib import Path\n"
        "from PIL import Image\n"
        "from pptx import Presentation\n"
        "def _r(p,o):\n"
        " o=Path(o); o.mkdir(parents=True,exist_ok=True); n=len(Presentation(p).slides)\n"
        " for i in range(1,n+1): Image.new('RGB',(16,9),'white').save(o/f'slide_{i:03d}.png')\n"
        " return n\n"
        "render_powerpoint=_r\nrender_libreoffice=_r\n",
        encoding="utf-8",
    )
    workflow_env = dict(os.environ)
    edit_env = dict(workflow_env)
    existing_pythonpath = edit_env.get("PYTHONPATH")
    edit_env["PYTHONPATH"] = (
        os.pathsep.join((str(fake), existing_pythonpath))
        if existing_pythonpath
        else str(fake)
    )

    # Sync cache identity and claim the accepted reconstruction using the
    # explicitly installed current workflow package, not repository code.
    workflow_python = Path(sys.executable)
    sync = subprocess.run(
        [str(workflow_python), "-m", "workflow_state", "next", "--project", str(project)],
        capture_output=True,
        text=True,
        env=workflow_env,
        check=False,
    )
    if sync.returncode:
        raise RuntimeError(sync.stdout + sync.stderr)
    request = json.loads(sync.stdout)
    attempt = request["requests"][0]["attempt"]
    # The generic editppt dispatcher belongs to image reconstruction runs, so
    # use the installed workflow CLI module for this current page lease.
    dispatched = subprocess.run(
        [
            str(workflow_python),
            "-m",
            "workflow_state",
            "dispatch",
            "--project",
            str(project),
            "--page",
            "1",
            "--agent",
            "portable",
            "--attempt",
            str(attempt),
        ],
        capture_output=True,
        text=True,
        env=workflow_env,
        check=False,
    )
    if dispatched.returncode:
        raise RuntimeError(dispatched.stdout + dispatched.stderr)
    descriptor = project / "07_editable" / "page_001.json"
    recorded = _run(
        [
            str(editppt),
            "run",
            "record",
            str(project),
            "--page",
            "1",
            "--agent-id",
            "portable",
            "--attempt",
            str(attempt),
            "--pptx",
            str(pptx),
            "--artifact",
            str(descriptor),
        ],
        env=edit_env,
    )
    finalized = _run([str(editppt), "run", "finalize", str(project)], env=edit_env)
    if recorded.get("state") != "complete" or finalized.get("status") != "complete":
        raise RuntimeError("portable record/finalize did not reach completion")
    if not (project / "08_final" / "deck.pptx").is_file():
        raise RuntimeError("portable final deck is missing")
    return {"record": recorded, "finalize": finalized}


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
