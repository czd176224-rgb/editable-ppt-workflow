"""Atomic, restart-safe idempotency ledger for every V5 external call."""

from __future__ import annotations

import copy
import json
import os
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Mapping

from workflow_v5_identity import semantic_identity


_LEDGER_VERSION = "request-ledger-v1"
_PURPOSES = frozenset({
    "comment_resolution",
    "material_search",
    "image2_design",
    "image2_design_qa",
    "editable_reconstruction",
    "final_slide_qa",
})
_TERMINAL = frozenset({"success", "negative"})


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False,
    ).encode("utf-8")


class RequestLedger:
    """Guarantee one executor per semantic request and reuse terminal outcomes."""

    def __init__(self, project: Path):
        self.project = Path(project).resolve()
        self.directory = self.project / "04_v5"
        self.path = self.directory / "request-ledger.json"
        self.lock_path = self.directory / "request-ledger.lock"

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
                    raise TimeoutError("timed out waiting for V5 request ledger lock")
                time.sleep(0.01)
        try:
            yield
        finally:
            self.lock_path.unlink(missing_ok=True)

    @staticmethod
    def _empty() -> dict[str, Any]:
        return {"ledger_version": _LEDGER_VERSION, "requests": {}}

    def _read(self) -> dict[str, Any]:
        if not self.path.is_file():
            return self._empty()
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError("V5 request ledger is unreadable") from exc
        if (
            not isinstance(value, dict)
            or value.get("ledger_version") != _LEDGER_VERSION
            or not isinstance(value.get("requests"), dict)
        ):
            raise ValueError("V5 request ledger is invalid")
        return value

    def _write(self, value: Mapping[str, Any]) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        temporary = self.directory / f".request-ledger.{uuid.uuid4().hex}.tmp"
        with temporary.open("xb") as handle:
            handle.write(_canonical(value))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, self.path)

    @staticmethod
    def _worker(worker_id: str) -> str:
        if not isinstance(worker_id, str) or not worker_id.strip():
            raise ValueError("request ledger worker_id is required")
        return worker_id

    @staticmethod
    def _key(purpose: str, inputs: Mapping[str, Any]) -> str:
        if purpose not in _PURPOSES:
            raise ValueError("external call purpose is not allowlisted")
        if not isinstance(inputs, Mapping):
            raise ValueError("external call semantic inputs must be an object")
        return semantic_identity(
            "external_request",
            contract_version=f"{purpose}-v1",
            semantic_inputs={"purpose": purpose, "request": inputs},
        )

    def claim(
        self, purpose: str, semantic_inputs: Mapping[str, Any], *, worker_id: str,
    ) -> dict[str, Any]:
        worker = self._worker(worker_id)
        key = self._key(purpose, semantic_inputs)
        with self._lock():
            ledger = self._read()
            entry = ledger["requests"].get(key)
            if isinstance(entry, dict) and entry.get("outcome") in _TERMINAL:
                return {
                    "decision": "reuse",
                    "request_key": key,
                    "outcome": entry["outcome"],
                    "result": copy.deepcopy(entry.get("result")),
                    **({"reason": entry["reason"]} if entry["outcome"] == "negative" else {}),
                }
            if isinstance(entry, dict) and entry.get("outcome") == "running":
                claimed_at = entry.get("claimed_at")
                if isinstance(claimed_at, (int, float)) and time.time() - claimed_at > 1800:
                    entry["outcome"] = "failed"
                    entry["worker_id"] = None
                    entry["reason"] = "expired execution lease recovered"
                else:
                    return {
                        "decision": "busy",
                        "request_key": key,
                        "worker_id": entry.get("worker_id"),
                    }
            attempts = int(entry.get("attempts", 0)) + 1 if isinstance(entry, dict) else 1
            ledger["requests"][key] = {
                "request_key": key,
                "purpose": purpose,
                "semantic_inputs": copy.deepcopy(dict(semantic_inputs)),
                "outcome": "running",
                "worker_id": worker,
                "claimed_at": time.time(),
                "attempts": attempts,
                "result": None,
                "reason": None,
            }
            self._write(ledger)
            return {"decision": "execute", "request_key": key, "attempt": attempts}

    def _complete(
        self, request_key: str, *, worker_id: str, outcome: str,
        result: Any, reason: str | None,
    ) -> None:
        worker = self._worker(worker_id)
        if outcome not in _TERMINAL:
            raise ValueError("request ledger terminal outcome is invalid")
        if outcome == "negative" and (not isinstance(reason, str) or not reason.strip()):
            raise ValueError("negative request result requires a reason")
        with self._lock():
            ledger = self._read()
            entry = ledger["requests"].get(request_key)
            if not isinstance(entry, dict) or entry.get("outcome") != "running":
                raise ValueError("request is not actively claimed")
            if entry.get("worker_id") != worker:
                raise ValueError("request ledger worker does not own this claim")
            entry["outcome"] = outcome
            entry["worker_id"] = None
            entry["claimed_at"] = None
            entry["result"] = copy.deepcopy(result)
            entry["reason"] = reason
            self._write(ledger)

    def complete_success(self, request_key: str, *, worker_id: str, result: Any) -> None:
        self._complete(
            request_key, worker_id=worker_id, outcome="success", result=result, reason=None,
        )

    def complete_negative(
        self, request_key: str, *, worker_id: str, reason: str, result: Any = None,
    ) -> None:
        self._complete(
            request_key, worker_id=worker_id, outcome="negative", result=result, reason=reason,
        )

    def fail_retryable(self, request_key: str, *, worker_id: str, reason: str) -> None:
        worker = self._worker(worker_id)
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("retryable request failure requires a reason")
        with self._lock():
            ledger = self._read()
            entry = ledger["requests"].get(request_key)
            if not isinstance(entry, dict) or entry.get("outcome") != "running":
                raise ValueError("request is not actively claimed")
            if entry.get("worker_id") != worker:
                raise ValueError("request ledger worker does not own this claim")
            entry["outcome"] = "failed"
            entry["worker_id"] = None
            entry["claimed_at"] = None
            entry["reason"] = reason
            self._write(ledger)

    def snapshot(self) -> dict[str, Any]:
        return self._read()
