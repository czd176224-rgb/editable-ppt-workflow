#!/usr/bin/env python3
"""Validate a public editable-ppt-workflow repository snapshot."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path


TEXT_SUFFIXES = {
    ".cmd",
    ".css",
    ".html",
    ".js",
    ".json",
    ".md",
    ".ps1",
    ".py",
    ".toml",
    ".txt",
    ".yaml",
    ".yml",
}
FORBIDDEN_SUFFIXES = {".docx", ".pptx", ".pdf", ".env"}
FORBIDDEN_DIRECTORY_PARTS = {".git", "__pycache__", ".pytest_cache"}
SKIP_CONTENT_SCAN = {"scripts/check_public_release.py"}


def normalized(relative: Path) -> str:
    return relative.as_posix()


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validate(root: Path) -> dict:
    errors: list[str] = []
    root = root.resolve()
    manifest_path = root / "plugins/editable-ppt-workflow/.codex-plugin/plugin.json"
    package_path = root / "package-info.json"
    marketplace_path = root / ".agents/plugins/marketplace.json"
    required = [manifest_path, package_path, marketplace_path, root / "LICENSE"]
    for path in required:
        if not path.is_file():
            errors.append(f"missing required file: {path.relative_to(root)}")

    if not errors:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
        package = json.loads(package_path.read_text(encoding="utf-8-sig"))
        marketplace = json.loads(marketplace_path.read_text(encoding="utf-8-sig"))
        if manifest.get("version") != package.get("pluginVersion"):
            errors.append("plugin manifest and package-info versions differ")
        if package.get("marketplace") != "editable-ppt-public":
            errors.append("package-info marketplace is not editable-ppt-public")
        if package.get("repository") != "czd176224-rgb/editable-ppt-workflow":
            errors.append("package-info repository is not the public repository")
        if package.get("repositoryVisibility") != "public":
            errors.append("package-info repositoryVisibility is not public")
        if marketplace.get("name") != "editable-ppt-public":
            errors.append("Marketplace name is not editable-ppt-public")
        entries = marketplace.get("plugins", [])
        if len(entries) != 1 or entries[0].get("name") != "editable-ppt-workflow":
            errors.append("Marketplace must expose exactly editable-ppt-workflow")

    token_pattern = re.compile(r"(?:gho_|github_pat_|sk-)[A-Za-z0-9_-]{12,}")
    user_path_pattern = re.compile(r"[A-Za-z]:\\Users\\[^\\\s]+", re.IGNORECASE)
    files: list[dict] = []
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        relative = path.relative_to(root)
        rel = normalized(relative)
        lowered = rel.lower()
        lowered_parts = {part.lower() for part in relative.parts}
        if lowered_parts.intersection(FORBIDDEN_DIRECTORY_PARTS) or lowered.startswith("docs/superpowers/"):
            errors.append(f"forbidden path: {rel}")
        if path.suffix.lower() in FORBIDDEN_SUFFIXES or path.name.lower() in {
            "auth.json",
            "config.yaml",
        }:
            errors.append(f"forbidden artifact: {rel}")
        if path.suffix.lower() in TEXT_SUFFIXES and rel not in SKIP_CONTENT_SCAN:
            text = path.read_text(encoding="utf-8-sig", errors="replace")
            if "editable-ppt-private" in text:
                errors.append(f"private Marketplace identifier in: {rel}")
            if token_pattern.search(text):
                errors.append(f"credential-like token in: {rel}")
            if user_path_pattern.search(text):
                errors.append(f"machine-specific user path in: {rel}")
        files.append({"path": rel, "bytes": path.stat().st_size, "sha256": sha256(path)})

    return {"schemaVersion": 1, "root": ".", "passed": not errors, "errors": errors, "files": files}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    parser.add_argument("--write-report", type=Path)
    args = parser.parse_args()
    report = validate(args.root)
    if args.write_report:
        args.write_report.parent.mkdir(parents=True, exist_ok=True)
        args.write_report.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    print(json.dumps({"passed": report["passed"], "errors": report["errors"], "fileCount": len(report["files"])}, ensure_ascii=False))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
