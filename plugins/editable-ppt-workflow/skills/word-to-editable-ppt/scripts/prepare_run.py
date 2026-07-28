"""Prepare a project from one paginated Word document."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

from jsonschema import Draft202012Validator

from build_page_contracts import build
from contract_version import CURRENT_CONTRACT
from extract_docx_pages import DEFAULT_MARKER, extract_auto
from init_project import initialize
from source_assets import extract_source_assets


WORKFLOW_CONTRACT_VERSION = CURRENT_CONTRACT


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _template_path() -> Path:
    template = Path(__file__).resolve().parents[1] / "template"
    if (template / "project.json").is_file():
        return template
    raise FileNotFoundError("Project template not found. Reinstall the plugin.")


def _atomic_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def _validate_project_config(config: dict) -> None:
    schema_path = Path(__file__).resolve().parents[1] / "schemas" / "project_config.schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    errors = sorted(Draft202012Validator(schema).iter_errors(config), key=lambda error: list(error.path))
    if errors:
        raise ValueError(f"project configuration validation failed: {errors[0].message}")


def prepare(word: Path, output: Path) -> dict:
    """Create a locked one-Word-page-to-one-slide project."""
    word = word.resolve()
    output = output.resolve()
    if not word.is_file() or word.suffix.lower() != ".docx":
        raise ValueError(f"Source must be an existing paginated DOCX file: {word}")

    pages_payload = extract_auto(word, DEFAULT_MARKER)
    page_count = pages_payload["page_count"]
    initialize(_template_path(), output, word.stem, page_count)

    config_path = output / "project.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["source_mode"] = "paginated_word"
    _validate_project_config(config)
    _atomic_json(config_path, config)

    source_copy = output / "00_source" / "source.docx"
    pages_path = output / "00_source" / "pages.json"
    shutil.copy2(word, source_copy)
    _atomic_json(pages_path, pages_payload)

    source_asset_manifest = extract_source_assets(source_copy, pages_payload, output)
    _atomic_json(output / "00_source" / "source_asset_manifest.json", source_asset_manifest)
    build(pages_path, output / "01_page_contracts", source_asset_manifest)

    jobs = [
        {
            "slide_id": f"slide_{page_number:03d}",
            "page_number": page_number,
            "status": "pending_style_confirmation",
            "contract_file": f"01_page_contracts/page_{page_number:03d}.json",
            "expected_output": f"06_images/generated/page_{page_number:03d}.png",
        }
        for page_number in range(1, page_count + 1)
    ]
    state = {
        "schema_version": "1.0",
        "workflow_contract_version": WORKFLOW_CONTRACT_VERSION,
        "project_name": word.stem,
        "created_at": now_iso(),
        "word_source": {
            "path": "00_source/source.docx",
            "sha256": sha256_file(source_copy),
            "pages_path": "00_source/pages.json",
            "pages_sha256": sha256_file(pages_path),
        },
        "pagination": {
            "mode": pages_payload["pagination_mode"],
            "backend": pages_payload.get("pagination_backend"),
            "page_count": page_count,
            "locked_page_order": list(range(1, page_count + 1)),
        },
        "style_confirmation": {"status": "pending", "confirmed_at": None},
        "jobs": jobs,
        "final_pptx": None,
    }
    _atomic_json(output / "workflow_run.json", state)
    return {
        "project": str(output),
        "workflow_contract_version": WORKFLOW_CONTRACT_VERSION,
        "page_count": page_count,
        "pagination_mode": pages_payload["pagination_mode"],
        "next_stage": "style_confirmation",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--word", type=Path, required=True, help="Required paginated DOCX.")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(prepare(args.word, args.output), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
