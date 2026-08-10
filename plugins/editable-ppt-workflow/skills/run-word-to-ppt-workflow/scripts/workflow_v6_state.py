"""Atomic persistence for the V6 project contract."""

from __future__ import annotations

import json
import os
import uuid
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Mapping

from workflow_v6_contract import validate_project


STATE_FILE = "workflow_v6.json"
LOCK_DIRECTORY = ".workflow_v6.lock"


def state_path(project: Path) -> Path:
    return Path(project).resolve() / STATE_FILE


def load(project: Path) -> dict[str, Any]:
    path = state_path(project)
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("V6 state root must be an object")
    validate_project(value)
    return value


def save(project: Path, value: Mapping[str, Any]) -> Path:
    validate_project(value)
    path = state_path(project)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex[:8]}.tmp")
    payload = json.dumps(value, ensure_ascii=False, indent=2) + "\n"
    try:
        temporary.write_text(payload, encoding="utf-8", newline="\n")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return path


def create(project: Path, value: Mapping[str, Any]) -> Path:
    path = state_path(project)
    if path.exists():
        raise FileExistsError(f"V6 state already exists: {path}")
    return save(project, value)


@contextmanager
def mutation_lock(project: Path, timeout: float = 30.0):
    lock = Path(project).resolve() / LOCK_DIRECTORY
    deadline = time.monotonic() + timeout
    while True:
        try:
            lock.mkdir()
            break
        except FileExistsError:
            if time.monotonic() >= deadline:
                raise TimeoutError("timed out acquiring V6 state mutation lock")
            time.sleep(0.05)
    try:
        yield
    finally:
        try:
            lock.rmdir()
        except FileNotFoundError:
            pass


def update_page(project: Path, page_number: int, page: Mapping[str, Any]) -> Path:
    """Atomically merge one page result without overwriting concurrent pages."""
    with mutation_lock(project):
        state = load(project)
        if page_number < 1 or page_number > len(state["pages"]):
            raise ValueError("V6 page number is out of range")
        if page.get("page_number") != page_number:
            raise ValueError("V6 page update identity is invalid")
        state["pages"][page_number - 1] = dict(page)
        return save(project, state)
