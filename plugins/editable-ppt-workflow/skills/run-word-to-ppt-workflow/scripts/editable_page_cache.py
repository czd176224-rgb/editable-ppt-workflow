"""Generic editable-page cache writer, isolated from the staged V4 generator."""

from __future__ import annotations

import hashlib
import os
import shutil
import json
from pathlib import Path
from typing import Any, Mapping

from cache_key import canonical_sha256
from cache_store import CacheHit, CacheStore
from page_pipeline import cache_hit, project_file
from workflow_contract import PAGE_CACHE_CONTRACT_VERSION


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def seal_completed_page(
    project: Path, job: Mapping[str, Any], artifact: Path,
    *, supporting_files: Mapping[str, Path] | None = None,
    logical_files: Mapping[str, Path] | None = None,
) -> CacheHit:
    """Seal a future editable-page backend artifact at its strict cache key."""
    project = Path(project).resolve()
    artifact = project_file(project, artifact)
    cache = job.get("cache")
    if not isinstance(cache, Mapping):
        raise ValueError("page cache identity has not been initialized")
    existing = cache_hit(project, cache)
    if existing is not None:
        return existing
    key = cache.get("key")
    identity = cache.get("identity")
    if not isinstance(key, str) or not isinstance(identity, Mapping) or canonical_sha256(identity) != key:
        raise ValueError("page cache identity is invalid")
    store = CacheStore(project)
    quarantine = store.quarantine_invalid("pages", key)
    try:
        with store.staging("pages", key) as staged:
            target = staged / "reconstruction" / artifact.name
            target.parent.mkdir()
            shutil.copy2(artifact, target)
            copied = [target]
            support = dict(supporting_files or {})
            for name, source in support.items():
                if Path(name).name != name or name in {"manifest.json", artifact.name}:
                    raise ValueError("editable page cache support filename is unsafe")
                source = project_file(project, source)
                destination = target.parent / name
                shutil.copy2(source, destination)
                copied.append(destination)
            logical_map = {}
            for logical, source in dict(logical_files or {}).items():
                if not isinstance(logical, str) or not logical or "\\" in logical:
                    raise ValueError("editable page cache logical path is unsafe")
                if logical.startswith("@schema/"):
                    suffix = logical.removeprefix("@schema/")
                    if Path(suffix).name != suffix:
                        raise ValueError("editable page cache schema name is unsafe")
                    source = Path(source).resolve()
                    destination = staged / "schemas" / suffix
                else:
                    parts = Path(logical).parts
                    if Path(logical).is_absolute() or ".." in parts or any(part in {".cache", ".private", ".workflow"} for part in parts):
                        raise ValueError("editable page cache logical path is unsafe")
                    if any(token in logical.casefold() for token in ("secret", "token", "nonce", ".key")):
                        raise ValueError("secret or nonce cannot enter editable page cache")
                    source = project_file(project, source)
                    destination = staged / "closure" / logical
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, destination)
                copied.append(destination)
                logical_map[logical] = {
                    "path": destination.relative_to(staged).as_posix(),
                    "sha256": _file_sha256(destination),
                }
            # Page-package descriptors must refer to the immutable cached PPTX,
            # not to the mutable page-work directory.
            if target.suffix.lower() == ".json" and "page.pptx" in support:
                payload = json.loads(target.read_text(encoding="utf-8"))
                final_pptx = store.root / "pages" / key / "reconstruction" / "page.pptx"
                payload["pptx"] = final_pptx.relative_to(project).as_posix()
                payload["pptx_sha256"] = _file_sha256(target.parent / "page.pptx")
                target.write_text(
                    json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
                    encoding="utf-8",
                )
            relative = target.relative_to(staged).as_posix()
            store.seal("pages", key, staged, {
                "artifact_version": PAGE_CACHE_CONTRACT_VERSION,
                "schema_version": 1,
                "cache_identity": dict(identity),
                "outputs": {"reconstruction": relative},
                "logical_files": logical_map,
                "files": [
                    {"path": path.relative_to(staged).as_posix(), "sha256": _file_sha256(path)}
                    for path in copied
                ],
            })
    except BaseException:
        if quarantine is not None and quarantine.exists():
            original = store.root / "pages" / key
            if not os.path.lexists(original):
                os.replace(quarantine, original)
        raise
    if quarantine is not None and quarantine.exists():
        shutil.rmtree(quarantine)
    hit = cache_hit(project, cache)
    if hit is None:
        raise RuntimeError("editable page cache entry was not sealed")
    return hit
