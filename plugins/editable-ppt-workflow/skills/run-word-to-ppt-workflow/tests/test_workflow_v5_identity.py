from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import workflow_v5_identity as identity  # noqa: E402
from workflow_v5_identity import ContentCatalog, semantic_identity  # noqa: E402


def test_semantic_identity_is_independent_of_project_location() -> None:
    first = semantic_identity(
        "design", contract_version="design-v1",
        semantic_inputs={"page": 2, "source_artifact_id": "sha256:abc", "style": "formal"},
    )
    second = semantic_identity(
        "design", contract_version="design-v1",
        semantic_inputs={"style": "formal", "source_artifact_id": "sha256:abc", "page": 2},
    )

    assert first == second
    with pytest.raises(ValueError, match="non-semantic"):
        semantic_identity(
            "design", contract_version="design-v1",
            semantic_inputs={"source_path": r"C:\\project\\source.docx"},
        )


def test_catalog_hashes_once_per_trust_boundary_and_warm_status_hashes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "project"
    source = project / "00_source" / "source.docx"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"locked source")
    calls = 0
    real_hash = identity._sha256_file

    def counted(path: Path) -> str:
        nonlocal calls
        calls += 1
        return real_hash(path)

    monkeypatch.setattr(identity, "_sha256_file", counted)
    catalog = ContentCatalog(project)

    first = catalog.record_file("word-source", source, boundary="ingestion")
    repeated = catalog.record_file("word-source", source, boundary="ingestion")
    status = catalog.status()
    upload = catalog.record_file("word-source", source, boundary="before_external_upload")

    assert calls == 2
    assert first["artifact_id"] == repeated["artifact_id"] == upload["artifact_id"]
    assert repeated["hash_performed"] is False
    assert upload["hash_performed"] is True
    assert status == {"artifacts": 1, "hash_operations": 1}


def test_catalog_survives_project_move_without_absolute_paths(tmp_path: Path) -> None:
    original = tmp_path / "original"
    source = original / "00_source" / "source.docx"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"portable project")
    before = ContentCatalog(original).record_file("word-source", source, boundary="ingestion")

    moved = tmp_path / "moved"
    shutil.move(str(original), str(moved))
    catalog = ContentCatalog(moved)
    snapshot = catalog.snapshot()
    after = catalog.record_file(
        "word-source", moved / "00_source" / "source.docx", boundary="ingestion",
    )

    assert before["artifact_id"] == after["artifact_id"]
    assert str(tmp_path) not in repr(snapshot)
    assert snapshot["entries"]["word-source"]["relative_path"] == "00_source/source.docx"

