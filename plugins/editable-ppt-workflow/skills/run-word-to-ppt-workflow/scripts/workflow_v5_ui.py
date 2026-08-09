"""Single-confirmation lifecycle and user-facing DAG event projection."""

from __future__ import annotations

import json
import os
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Mapping


_UI_STATE_VERSION = "v5-ui-lifecycle-v1"
_STAGE = {
    "source_lock": ("preparing", "正在准备分页内容"),
    "intent": ("preparing", "正在理解每页要求"),
    "style": ("preparing", "正在应用已确认的视觉合同"),
    "material": ("finding_materials", "正在查找明确要求的真实素材"),
    "design": ("designing", "正在设计幻灯片"),
    "compose": ("designing", "正在组合版式与真实素材"),
    "reconstruct": ("making_editable", "正在将页面重建为可编辑对象"),
    "page_validate": ("checking", "正在检查页面结构"),
    "visual_qa": ("checking", "正在检查最终页面效果"),
    "assemble": ("checking", "正在按原顺序装配演示文稿"),
    "office_validate": ("checking", "正在完成最终 Office 检查"),
}


class ConfirmationLifecycle:
    def __init__(self, project: Path):
        self.project = Path(project).resolve()
        self.directory = self.project / "04_v5"
        self.path = self.directory / "ui-state.json"
        self.lock_path = self.directory / "ui-state.lock"

    @contextmanager
    def _lock(self) -> Iterator[None]:
        self.directory.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + 10.0
        while True:
            try:
                descriptor = os.open(self.lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
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
                    raise TimeoutError("V5 UI lifecycle lock timed out")
                time.sleep(0.01)
        try:
            yield
        finally:
            self.lock_path.unlink(missing_ok=True)

    @staticmethod
    def _empty() -> dict[str, Any]:
        return {
            "artifact_version": _UI_STATE_VERSION,
            "status": "pending",
            "browser_launched": False,
            "launch_session_id": None,
            "launched_at": None,
            "contract_id": None,
        }

    def _read(self) -> dict[str, Any]:
        if not self.path.is_file():
            return self._empty()
        value = json.loads(self.path.read_text(encoding="utf-8"))
        if (
            not isinstance(value, dict)
            or value.get("artifact_version") != _UI_STATE_VERSION
            or value.get("status") not in {"pending", "confirmed"}
        ):
            raise ValueError("V5 UI lifecycle state is invalid")
        return value

    def _write(self, value: Mapping[str, Any]) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        temporary = self.directory / f".ui-state.{uuid.uuid4().hex}.tmp"
        temporary.write_text(
            json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            encoding="utf-8",
        )
        os.replace(temporary, self.path)

    def snapshot(self) -> dict[str, Any]:
        state = self._read()
        return {
            "enabled": True,
            "status": state["status"],
            "browser_launched": state["browser_launched"],
        }

    def claim_browser_launch(self, *, session_id: str, force: bool = False) -> dict[str, Any]:
        if not isinstance(session_id, str) or not session_id.strip():
            raise ValueError("V5 UI session_id is required")
        with self._lock():
            state = self._read()
            if state["status"] == "confirmed":
                return {"open_browser": False, "reason": "already_confirmed"}
            launched_at = state.get("launched_at")
            launch_fresh = (
                state.get("browser_launched") is True
                and isinstance(launched_at, (int, float))
                and time.time() - launched_at < 120
            )
            if launch_fresh and not force:
                return {"open_browser": False, "reason": "already_launched"}
            state["browser_launched"] = True
            state["launch_session_id"] = session_id
            state["launched_at"] = time.time()
            self._write(state)
            return {"open_browser": True, "reason": "first_confirmation_launch"}

    def record_launch_result(self, *, session_id: str, success: bool) -> None:
        if type(success) is not bool:
            raise ValueError("V5 browser launch result must be boolean")
        with self._lock():
            state = self._read()
            if state.get("launch_session_id") != session_id:
                return
            if not success:
                state["browser_launched"] = False
                state["launch_session_id"] = None
                state["launched_at"] = None
                self._write(state)

    def begin_reconfirmation(self) -> None:
        with self._lock():
            state = self._read()
            state.update({
                "status": "pending", "contract_id": None,
                "browser_launched": False, "launch_session_id": None, "launched_at": None,
            })
            self._write(state)

    def confirm(self, contract_id: str) -> dict[str, Any]:
        if not isinstance(contract_id, str) or not contract_id.strip():
            raise ValueError("V5 style contract_id is required")
        with self._lock():
            state = self._read()
            if state["status"] == "confirmed":
                if state["contract_id"] != contract_id:
                    raise ValueError("style change requires explicit reconfirmation")
                return {"confirmed_now": False, "contract_id": contract_id}
            state["status"] = "confirmed"
            state["contract_id"] = contract_id
            self._write(state)
            return {"confirmed_now": True, "contract_id": contract_id}


def _project_event(raw: Mapping[str, Any], *, diagnostics: bool) -> dict[str, Any]:
    kind = raw.get("kind")
    stage, label = _STAGE.get(kind, ("preparing", "正在准备项目"))
    if kind == "office_validate" and raw.get("status") == "complete":
        stage, label = "complete", "演示文稿已完成"
    event = {
        "stage": stage,
        "label": label,
        "status": raw.get("status", "pending"),
        "page_number": raw.get("page_number"),
    }
    if diagnostics:
        event["technical"] = {
            "event": raw.get("event"),
            "node_id": raw.get("node_id"),
            "kind": kind,
            "timestamp": raw.get("timestamp"),
        }
    return event


def read_progress_events(
    project: Path, *, cursor: int = 0, diagnostics: bool = False,
) -> dict[str, Any]:
    if type(cursor) is not int or cursor < 0:
        raise ValueError("V5 progress cursor must be a non-negative integer")
    path = Path(project).resolve() / "04_v5" / "dag-events.jsonl"
    if not path.is_file():
        return {"events": [], "next_cursor": cursor}
    size = path.stat().st_size
    if cursor > size:
        raise ValueError("V5 progress cursor is beyond the event stream")
    events: list[dict[str, Any]] = []
    next_cursor = cursor
    with path.open("rb") as stream:
        stream.seek(cursor)
        for line in stream:
            next_cursor = stream.tell()
            try:
                raw = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError("V5 DAG event stream is invalid") from exc
            if not isinstance(raw, Mapping):
                raise ValueError("V5 DAG event must be an object")
            events.append(_project_event(raw, diagnostics=diagnostics))
    return {"events": events, "next_cursor": next_cursor}
