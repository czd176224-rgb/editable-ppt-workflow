"""Trust-boundary content catalog and path-independent identities for V5."""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Mapping


_CATALOG_VERSION = "content-catalog-v1"
_TRUST_BOUNDARIES = frozenset({
    "ingestion",
    "before_external_upload",
    "after_external_output",
    "before_final_assembly",
    "before_delivery",
})
_NON_SEMANTIC_KEYS = frozenset({
    "path", "absolute_path", "timestamp", "created_at", "updated_at",
    "nonce", "signature", "receipt", "receipts", "receipt_graph",
})
_WINDOWS_ABSOLUTE = re.compile(r"^(?:[A-Za-z]:[\\/]|\\\\)")


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False,
    ).encode("utf-8")


def _semantic_value(value: Any, *, field: str = "semantic_inputs") -> Any:
    if value is None or isinstance(value, (bool, int, str)):
        if isinstance(value, str) and (value.startswith("/") or _WINDOWS_ABSOLUTE.match(value)):
            raise ValueError(f"non-semantic absolute path is forbidden in {field}")
        return value
    if isinstance(value, float):
        if value != value or value in {float("inf"), float("-inf")}:
            raise ValueError(f"non-finite value is forbidden in {field}")
        return value
    if isinstance(value, list):
        return [_semantic_value(item, field=field) for item in value]
    if isinstance(value, Mapping):
        normalized: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str) or not key:
                raise ValueError(f"non-semantic object key in {field}")
            lowered = key.lower()
            if (
                lowered in _NON_SEMANTIC_KEYS
                or lowered.endswith("_path")
                or lowered.endswith("_timestamp")
            ):
                raise ValueError(f"non-semantic field is forbidden in identity: {key}")
            normalized[key] = _semantic_value(item, field=f"{field}.{key}")
        return normalized
    raise ValueError(f"non-semantic JSON value in {field}")


def semantic_identity(
    kind: str, *, contract_version: str, semantic_inputs: Mapping[str, Any],
) -> str:
    """Hash only canonical semantic inputs; never paths, clocks, or receipts."""
    if not isinstance(kind, str) or not kind.strip():
        raise ValueError("identity kind is required")
    if not isinstance(contract_version, str) or not contract_version.strip():
        raise ValueError("identity contract_version is required")
    if not isinstance(semantic_inputs, Mapping):
        raise ValueError("semantic_inputs must be an object")
    payload = {
        "kind": kind,
        "contract_version": contract_version,
        "semantic_inputs": _semantic_value(semantic_inputs),
    }
    return hashlib.sha256(_canonical(payload)).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _fingerprint(path: Path) -> dict[str, int]:
    stat = path.stat()
    return {
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "device": stat.st_dev,
        "file_id": stat.st_ino,
    }


class ContentCatalog:
    """Record byte identity once per explicit trust boundary."""

    def __init__(self, project: Path):
        self.project = Path(project).resolve()
        self.directory = self.project / "04_v5"
        self.path = self.directory / "content-catalog.json"
        self.lock_path = self.directory / "content-catalog.lock"

    @contextmanager
    def _lock(self, timeout: float = 30.0) -> Iterator[None]:
        self.directory.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + timeout
        while True:
            try:
                descriptor = os.open(self.lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(descriptor, uuid.uuid4().hex.encode("ascii"))
                os.close(descriptor)
                break
            except (FileExistsError, PermissionError) as exc:
                try:
                    if time.time() - self.lock_path.stat().st_mtime > 120:
                        self.lock_path.unlink(missing_ok=True)
                        continue
                except FileNotFoundError:
                    continue
                if time.monotonic() >= deadline:
                    if isinstance(exc, PermissionError) and not self.lock_path.exists():
                        raise
                    raise TimeoutError("timed out waiting for V5 content catalog lock")
                time.sleep(0.01)
        try:
            yield
        finally:
            self.lock_path.unlink(missing_ok=True)

    @staticmethod
    def _empty() -> dict[str, Any]:
        return {
            "catalog_version": _CATALOG_VERSION,
            "hash_operations": 0,
            "entries": {},
        }

    def _read(self) -> dict[str, Any]:
        if not self.path.is_file():
            return self._empty()
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError("V5 content catalog is unreadable") from exc
        if (
            not isinstance(value, dict)
            or value.get("catalog_version") != _CATALOG_VERSION
            or type(value.get("hash_operations")) is not int
            or not isinstance(value.get("entries"), dict)
        ):
            raise ValueError("V5 content catalog is invalid")
        return value

    def _write(self, value: Mapping[str, Any]) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        temporary = self.directory / f".content-catalog.{uuid.uuid4().hex}.tmp"
        data = _canonical(value)
        with temporary.open("xb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, self.path)

    def _project_file(self, value: Path) -> tuple[Path, str]:
        path = Path(value).resolve()
        try:
            relative = path.relative_to(self.project).as_posix()
        except ValueError as exc:
            raise ValueError("catalog artifact must be project-local") from exc
        if not path.is_file() or path.is_symlink():
            raise ValueError("catalog artifact must be a regular project file")
        return path, relative

    def record_file(self, logical_id: str, path: Path, *, boundary: str) -> dict[str, Any]:
        if not isinstance(logical_id, str) or not logical_id.strip():
            raise ValueError("catalog logical_id is required")
        if boundary not in _TRUST_BOUNDARIES:
            raise ValueError("unknown V5 trust boundary")
        actual, relative = self._project_file(path)
        for _attempt in range(2):
            current_fingerprint = _fingerprint(actual)
            with self._lock():
                catalog = self._read()
                entry = catalog["entries"].get(logical_id)
                verification = (
                    entry.get("boundaries", {}).get(boundary)
                    if isinstance(entry, dict) else None
                )
                if (
                    isinstance(entry, dict)
                    and entry.get("relative_path") == relative
                    and isinstance(verification, dict)
                    and verification.get("fingerprint") == current_fingerprint
                ):
                    return {
                        "logical_id": logical_id,
                        "artifact_id": entry["artifact_id"],
                        "content_sha256": entry["content_sha256"],
                        "boundary": boundary,
                        "hash_performed": False,
                    }

            # Large-file I/O is deliberately outside the project lock.
            digest = _sha256_file(actual)
            stable_fingerprint = _fingerprint(actual)
            if stable_fingerprint != current_fingerprint:
                continue
            with self._lock():
                if _fingerprint(actual) != stable_fingerprint:
                    continue
                catalog = self._read()
                entry = catalog["entries"].get(logical_id)
                boundaries = (
                    dict(entry.get("boundaries", {}))
                    if isinstance(entry, dict) and entry.get("content_sha256") == digest
                    else {}
                )
                boundaries[boundary] = {
                    "fingerprint": stable_fingerprint,
                    "content_sha256": digest,
                }
                catalog["entries"][logical_id] = {
                    "logical_id": logical_id,
                    "relative_path": relative,
                    "artifact_id": f"sha256:{digest}",
                    "content_sha256": digest,
                    "size": stable_fingerprint["size"],
                    "boundaries": boundaries,
                }
                catalog["hash_operations"] += 1
                self._write(catalog)
                return {
                    "logical_id": logical_id,
                    "artifact_id": f"sha256:{digest}",
                    "content_sha256": digest,
                    "boundary": boundary,
                    "hash_performed": True,
                }
        raise ValueError("catalog artifact changed while being hashed")

    def snapshot(self) -> dict[str, Any]:
        """Return catalog metadata only; this method never opens artifact files."""
        return self._read()

    def status(self) -> dict[str, int]:
        catalog = self._read()
        return {
            "artifacts": len(catalog["entries"]),
            "hash_operations": catalog["hash_operations"],
        }
