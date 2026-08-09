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
from evidence_index import build_evidence_index
from evidence_retrieval import retrieve_page_evidence
from page_fact_plan import build_fact_plan
import workflow_state
from page_coverage import build_coverage_contract
from style_recommendations import build_recommendations
from workflow_contract import version_vector


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


def _prepare_locked(word: Path, output: Path, logo: Path | None = None) -> dict:
    """Create one project while the caller owns its bootstrap lock."""
    word = word.resolve()
    if not word.is_file() or word.suffix.lower() != ".docx":
        raise ValueError(f"Source must be an existing paginated DOCX file: {word}")
    if logo is None:
        raise ValueError("Logo is required and must be supplied as an SVG file.")
    logo = Path(logo).resolve()
    if not logo.is_file() or logo.suffix.lower() != ".svg":
        raise ValueError(f"Logo must be an existing SVG file: {logo}")

    pages_payload = extract_auto(word, DEFAULT_MARKER)
    page_count = pages_payload["page_count"]
    initialize(_template_path(), output, word.stem, page_count)

    config_path = output / "project.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["workflow_contract_version"] = WORKFLOW_CONTRACT_VERSION
    config["source_mode"] = "paginated_word"
    _validate_project_config(config)
    _atomic_json(config_path, config)

    source_copy = output / "00_source" / "source.docx"
    pages_path = output / "00_source" / "pages.json"
    source_copy.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(word, source_copy)
    logo_copy = output / "00_source" / "company_logo.svg"
    shutil.copy2(logo, logo_copy)
    logo_source = {
        "path": "00_source/company_logo.svg",
        "sha256": sha256_file(logo_copy),
        "media_type": "image/svg+xml",
    }
    _atomic_json(pages_path, pages_payload)

    source_asset_manifest = extract_source_assets(source_copy, pages_payload, output)
    _atomic_json(output / "00_source" / "source_asset_manifest.json", source_asset_manifest)
    build(pages_path, output / "01_page_contracts", source_asset_manifest)

    evidence_index = build_evidence_index(output, source_asset_manifest)
    _atomic_json(output / "03_evidence" / "evidence_index.json", evidence_index)
    page_artifacts: dict[int, dict[str, str]] = {}
    for page_number in range(1, page_count + 1):
        contract_path = output / "01_page_contracts" / f"page_{page_number:03d}.json"
        contract = json.loads(contract_path.read_text(encoding="utf-8"))
        page_index = evidence_index["pages"].get(str(page_number), {"chunks": []})
        selected = retrieve_page_evidence(contract, page_index)
        fact_plan = build_fact_plan(contract, selected)
        coverage = build_coverage_contract(contract, fact_plan)
        page_dir = output / "03_evidence" / f"page_{page_number:03d}"
        selected_path = page_dir / "selected_evidence.json"
        fact_path = page_dir / "fact_plan.json"
        coverage_path = page_dir / "coverage_contract.json"
        _atomic_json(selected_path, selected)
        _atomic_json(fact_path, fact_plan)
        _atomic_json(coverage_path, coverage)
        page_artifacts[page_number] = {
            "selected_evidence_file": selected_path.relative_to(output).as_posix(),
            "selected_evidence_sha256": sha256_file(selected_path),
            "fact_plan_file": fact_path.relative_to(output).as_posix(),
            "fact_plan_sha256": sha256_file(fact_path),
            "coverage_contract_file": coverage_path.relative_to(output).as_posix(),
            "coverage_sha256": coverage["sha256"],
        }

    jobs = [
        {
            "slide_id": f"slide_{page_number:03d}",
            "page_number": page_number,
            "status": "pending_style_confirmation",
            "generation_calls": 0,
            "reconstruction_calls": 0,
            "semantic_calls": 0,
            "contract_file": f"01_page_contracts/page_{page_number:03d}.json",
            "expected_output": f"06_images/generated/page_{page_number:03d}.png",
            **page_artifacts[page_number],
        }
        for page_number in range(1, page_count + 1)
    ]
    state = {
        "schema_version": "1.0",
        **version_vector(),
        "project_name": word.stem,
        "created_at": now_iso(),
        "word_source": {
            "path": "00_source/source.docx",
            "sha256": sha256_file(source_copy),
            "pages_path": "00_source/pages.json",
            "pages_sha256": sha256_file(pages_path),
        },
        "logo_source": logo_source,
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
    workflow_state.initialize(output, state)
    build_recommendations(output)
    return {
        "project": str(output),
        "workflow_contract_version": WORKFLOW_CONTRACT_VERSION,
        "page_count": page_count,
        "pagination_mode": pages_payload["pagination_mode"],
        "next_stage": "style_confirmation",
    }


def prepare(word: Path, output: Path, logo: Path | None = None) -> dict:
    """Create a locked one-Word-page-to-one-slide project with a required SVG logo."""
    with workflow_state.project_bootstrap_lock(output, timeout_seconds=900.0) as locked_output:
        return _prepare_locked(word, locked_output, logo)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--word", type=Path, required=True, help="Required paginated DOCX.")
    parser.add_argument("--logo", type=Path, required=True, help="Required SVG company logo for the fixed frame.")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(prepare(args.word, args.output, logo=args.logo), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
