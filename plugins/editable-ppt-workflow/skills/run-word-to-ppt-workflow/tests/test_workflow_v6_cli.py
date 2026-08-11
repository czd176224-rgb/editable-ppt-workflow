import json
import sys
from pathlib import Path

import pytest


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import workflow_v6_cli  # noqa: E402
from workflow_v6_contract import new_page, new_project  # noqa: E402
from workflow_v6_materials import new_page_materials  # noqa: E402
from workflow_v6_source import fail_reference, import_reference, reject_reference  # noqa: E402
from workflow_v6_state import create  # noqa: E402


def _project_with_pending_reference(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    state = new_project(
        word_source={"path": "00_source/source.docx", "sha256": "a" * 64},
        logo_source={"path": "00_source/logo.svg", "sha256": "b" * 64},
        pages=[new_page(1, title="Evidence")],
    )
    create(project, state)
    materials = new_page_materials(
        page_number=1, fixed_page_title="Evidence", word_original="Evidence", effective_body="",
    )
    material_path = project / "02_v6/page_materials/page_001.json"
    material_path.parent.mkdir(parents=True, exist_ok=True)
    material_path.write_text(json.dumps(materials), encoding="utf-8")
    reference_path = project / "02_v6/reference_materials/page_001.json"
    reference_path.parent.mkdir(parents=True, exist_ok=True)
    reference_path.write_text(json.dumps({
        "artifact_version": "reference-materials-v6",
        "page_number": 1,
        "references": [],
        "search_requests": [],
        "reference_acquisitions": [{
            "request_id": "request-1",
            "page_number": 1,
            "purpose": "verified storefront image",
            "identity_evidence_need": "show the named storefront",
            "status": "pending",
            "history": ["pending"],
        }],
    }), encoding="utf-8")
    return project


def test_production_dispatcher_exposes_only_v6_and_diagnostics():
    namespace = {}
    source = (SCRIPTS / "word_to_editable_ppt.py").read_text(encoding="utf-8")
    exec(compile(source, "word_to_editable_ppt.py", "exec"), namespace)
    assert namespace["TOOLS"] == {
        "confirm-ui": "confirm_ui/server.py",
        "doctor": "doctor.py",
        "v6": "workflow_v6_cli.py",
    }


def test_v6_cli_does_not_import_legacy_workflows():
    source = (SCRIPTS / "workflow_v6_cli.py").read_text(encoding="utf-8")
    assert "workflow_v4" not in source
    assert "workflow_v5" not in source


def test_cli_exposes_local_import_failure_and_found_rejection_commands_without_a_url_fetch():
    parser = workflow_v6_cli._parser()

    imported = parser.parse_args([
        "import-reference", "--project", "project", "--page", "1", "--request-id", "request-1",
        "--image", "chosen.png", "--source-url", "http://127.0.0.1/private.png",
    ])
    failed = parser.parse_args([
        "fail-reference", "--project", "project", "--page", "1", "--request-id", "request-1",
        "--reason", "not found",
    ])
    rejected = parser.parse_args([
        "reject-reference", "--project", "project", "--page", "1", "--request-id", "request-1",
        "--reason", "not suitable",
    ])

    assert imported.command == "import-reference"
    assert imported.source_url == "http://127.0.0.1/private.png"
    assert failed.command == "fail-reference"
    assert rejected.command == "reject-reference"


def test_import_reference_records_found_candidate_and_reject_api_closes_it_without_url_fetch(
    tmp_path: Path, monkeypatch,
):
    """Dereferencing a selected source URL here would reopen the Python SSRF surface."""
    project = _project_with_pending_reference(tmp_path)
    image = tmp_path / "selected.png"
    image.write_bytes(b"locally supplied image bytes")
    monkeypatch.setattr("socket.create_connection", lambda *_args, **_kwargs: pytest.fail("URL was fetched"))

    result = import_reference(
        project, page_number=1, request_id="request-1", image=image,
        source_url="http://127.0.0.1/private.png",
    )

    assert result["status"] == "found"
    receipt = json.loads((project / "02_v6/reference_materials/page_001.json").read_text(encoding="utf-8"))
    acquisition = receipt["reference_acquisitions"][0]
    assert acquisition["history"] == ["pending", "found"]
    material = json.loads((project / "02_v6/page_materials/page_001.json").read_text(encoding="utf-8"))
    assert material["reference_images"] == []
    assert acquisition["candidate"]["source_url"] == "http://127.0.0.1/private.png"
    assert acquisition["candidate"]["local_path"].startswith("02_v6/reference_images/")

    rejected = reject_reference(project, page_number=1, request_id="request-1", reason="not suitable")
    assert rejected["status"] == "user_rejected"


def test_failed_no_retry_reference_refuses_a_second_result(tmp_path: Path):
    """Retrying after a terminal result would violate the one-shot evidence decision."""
    project = _project_with_pending_reference(tmp_path)
    image = tmp_path / "late.png"
    image.write_bytes(b"late local image")

    result = fail_reference(project, page_number=1, request_id="request-1", reason="not found")

    assert result["status"] == "failed_no_retry"
    with pytest.raises(ValueError, match="terminal"):
        import_reference(project, page_number=1, request_id="request-1", image=image, source_url=None)


def test_found_reference_can_be_user_rejected_without_retrying_search(tmp_path: Path):
    """A reviewer rejection closes the found candidate rather than issuing another search."""
    project = _project_with_pending_reference(tmp_path)
    image = tmp_path / "found.png"
    image.write_bytes(b"found local image")
    import_reference(project, page_number=1, request_id="request-1", image=image, source_url=None)
    receipt_path = project / "02_v6/reference_materials/page_001.json"

    result = reject_reference(project, page_number=1, request_id="request-1", reason="user_rejected")

    assert result["status"] == "user_rejected"
    saved = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert saved["reference_acquisitions"][0]["history"] == ["pending", "found", "user_rejected"]
