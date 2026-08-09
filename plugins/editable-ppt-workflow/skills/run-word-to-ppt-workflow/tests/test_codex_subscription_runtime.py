from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from codex_subscription_runtime import (  # noqa: E402
    CodexStructuredResult,
    CodexRuntimeUnavailable,
    _effort_override,
    app_server_pool_stats,
    invoke_structured,
    shutdown_app_server_pool,
)
import v4_qa_gateway  # noqa: E402
import workflow_state  # noqa: E402
from current_contract_fixture import write_valid_generation_receipt  # noqa: E402
from test_independent_page_workflow import _project  # noqa: E402


OUTPUT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["status"],
    "properties": {"status": {"type": "string", "const": "complete"}},
}


@pytest.fixture(autouse=True)
def _clean_app_server_pool():
    shutdown_app_server_pool()
    yield
    shutdown_app_server_pool()


def _fake_server(tmp_path: Path, *, account_type: str = "chatgpt", mode: str = "success") -> list[str]:
    script = tmp_path / "fake_app_server.py"
    script.write_text(
        """
import json, sys, time

account_type = sys.argv[1]
mode = sys.argv[2]

def send(value):
    sys.stdout.write(json.dumps(value, separators=(\",\", \":\")) + \"\\n\")
    sys.stdout.flush()

for line in sys.stdin:
    request = json.loads(line)
    method = request.get(\"method\")
    request_id = request.get(\"id\")
    if method == \"initialize\":
        send({\"id\": request_id, \"result\": {\"userAgent\": \"fake\", \"platformFamily\": \"windows\", \"platformOs\": \"windows\"}})
    elif method == \"initialized\":
        continue
    elif method == \"account/read\":
        account = {\"type\": \"chatgpt\", \"email\": \"hidden@example.invalid\", \"planType\": \"plus\"} if account_type == \"chatgpt\" else {\"type\": account_type}
        send({\"id\": request_id, \"result\": {\"account\": account, \"requiresOpenaiAuth\": True}})
    elif method == \"thread/start\":
        send({\"id\": request_id, \"result\": {\"thread\": {\"id\": \"thr_test\"}, \"model\": \"gpt-test\", \"modelProvider\": \"openai\", \"approvalPolicy\": \"never\", \"approvalsReviewer\": \"user\", \"cwd\": request[\"params\"][\"cwd\"], \"sandbox\": {\"type\": \"readOnly\"}}})
    elif method == \"turn/start\":
        if mode == \"timeout\":
            time.sleep(60)
        send({\"id\": request_id, \"result\": {\"turn\": {\"id\": \"turn_test\", \"status\": \"inProgress\", \"items\": []}}})
        if mode == \"malformed\":
            text = \"not-json\"
        else:
            text = json.dumps({\"status\": \"complete\"})
        send({\"method\": \"item/completed\", \"params\": {\"threadId\": \"thr_test\", \"turnId\": \"turn_test\", \"item\": {\"type\": \"agentMessage\", \"id\": \"item_test\", \"text\": text, \"phase\": \"final\"}}})
        send({\"method\": \"turn/completed\", \"params\": {\"threadId\": \"thr_test\", \"turn\": {\"id\": \"turn_test\", \"status\": \"completed\", \"items\": [], \"error\": None, \"usage\": {\"inputTokens\": 11, \"cachedInputTokens\": 3, \"outputTokens\": 5}}}})
""".strip()
        + "\n",
        encoding="utf-8",
    )
    return [sys.executable, str(script), account_type, mode]


def _observable_server(
    tmp_path: Path,
    *,
    delay: float = 0,
    fail_first: bool = False,
    startup_delay: float = 0,
) -> tuple[list[str], Path]:
    script = tmp_path / "observable_app_server.py"
    starts = tmp_path / "starts.log"
    marker = tmp_path / "failed-once.marker"
    script.write_text(
        """
import json, pathlib, sys, time

starts = pathlib.Path(sys.argv[1])
marker = pathlib.Path(sys.argv[2])
delay = float(sys.argv[3])
fail_first = sys.argv[4] == "true"
startup_delay = float(sys.argv[5])
with starts.open("a", encoding="utf-8") as stream:
    stream.write("start\\n")
time.sleep(startup_delay)

def send(value):
    sys.stdout.write(json.dumps(value, separators=(",", ":")) + "\\n")
    sys.stdout.flush()

for line in sys.stdin:
    request = json.loads(line)
    method = request.get("method")
    request_id = request.get("id")
    if method == "initialize":
        send({"id": request_id, "result": {}})
    elif method == "initialized":
        continue
    elif method == "account/read":
        send({"id": request_id, "result": {"account": {"type": "chatgpt", "planType": "plus"}}})
    elif method == "thread/start":
        send({"id": request_id, "result": {"thread": {"id": "thr_" + str(request_id)}, "model": "gpt-test", "modelProvider": "openai"}})
    elif method == "turn/start":
        if fail_first and not marker.exists():
            marker.write_text("failed", encoding="utf-8")
            sys.exit(7)
        time.sleep(delay)
        turn_id = "turn_" + str(request_id)
        thread_id = request["params"]["threadId"]
        send({"id": request_id, "result": {"turn": {"id": turn_id}}})
        send({"method": "item/completed", "params": {"item": {"type": "agentMessage", "text": json.dumps({"status": "complete"})}}})
        send({"method": "turn/completed", "params": {"turn": {"id": turn_id, "error": None, "usage": {}}}})
""".strip()
        + "\n",
        encoding="utf-8",
    )
    return [
        sys.executable,
        str(script),
        str(starts),
        str(marker),
        str(delay),
        str(fail_first).lower(),
        str(startup_delay),
    ], starts


def _invoke(project: Path, command: list[str], *, timeout: float = 5) -> CodexStructuredResult:
    return invoke_structured(
        project,
        role="qa",
        prompt="Review.",
        images=[],
        output_schema=OUTPUT_SCHEMA,
        timeout=timeout,
        command=command,
    )


def test_sequential_calls_reuse_initialized_chatgpt_lane(tmp_path: Path) -> None:
    command, starts = _observable_server(tmp_path)

    first = _invoke(tmp_path, command)
    second = _invoke(tmp_path, command)

    assert starts.read_text(encoding="utf-8").splitlines() == ["start"]
    assert first.startup_reused is False
    assert second.startup_reused is True
    assert first.safe_trace["startup_reused"] is False
    assert second.safe_trace["startup_reused"] is True
    assert first.thread_id != second.thread_id
    assert app_server_pool_stats()["process_starts"] == 1
    assert app_server_pool_stats()["reused_leases"] == 1


def test_pool_uses_bounded_exclusive_lanes_for_concurrent_calls(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("EDITABLE_PPT_CODEX_APP_SERVER_POOL_SIZE", "2")
    command, _starts = _observable_server(tmp_path, delay=0.4, startup_delay=0.2)
    results: list[CodexStructuredResult] = []

    threads = [threading.Thread(target=lambda: results.append(_invoke(tmp_path, command))) for _ in range(3)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert all(not thread.is_alive() for thread in threads)
    assert len(results) == 3
    stats = app_server_pool_stats()
    assert stats["process_starts"] == 2
    assert stats["live_lanes"] == 2
    assert stats["max_lanes_per_key"] == 2


def test_unhealthy_lane_is_evicted_and_restarted_once(tmp_path: Path) -> None:
    command, starts = _observable_server(tmp_path, fail_first=True)

    result = _invoke(tmp_path, command)

    assert result.value == {"status": "complete"}
    assert starts.read_text(encoding="utf-8").splitlines() == ["start", "start"]
    stats = app_server_pool_stats()
    assert stats["evictions"] == 1
    assert stats["recoveries"] == 1


def test_explicit_pool_shutdown_closes_project_lanes(tmp_path: Path) -> None:
    command, starts = _observable_server(tmp_path)
    _invoke(tmp_path, command)

    assert app_server_pool_stats()["live_lanes"] == 1
    shutdown_app_server_pool(project=tmp_path, command=command)
    assert app_server_pool_stats()["live_lanes"] == 0

    _invoke(tmp_path, command)
    assert starts.read_text(encoding="utf-8").splitlines() == ["start", "start"]


def test_chatgpt_app_server_returns_schema_validated_result_and_safe_trace(tmp_path: Path) -> None:
    image = tmp_path / "body.png"
    image.write_bytes(b"png")

    result = invoke_structured(
        tmp_path,
        role="qa",
        prompt="Review the sealed page.",
        images=[image],
        output_schema=OUTPUT_SCHEMA,
        timeout=5,
        command=_fake_server(tmp_path),
    )

    assert result.value == {"status": "complete"}
    assert result.thread_id == "thr_test"
    assert result.turn_id == "turn_test"
    assert result.model == "gpt-test"
    assert result.model_provider == "openai"
    assert result.auth_mode == "chatgpt"
    assert result.plan_type == "plus"
    assert result.usage == {"inputTokens": 11, "cachedInputTokens": 3, "outputTokens": 5}
    serialized = json.dumps(result.safe_trace, sort_keys=True)
    assert "hidden@example.invalid" not in serialized
    assert "accessToken" not in serialized


def test_api_key_auth_is_rejected_before_a_model_turn(tmp_path: Path) -> None:
    with pytest.raises(CodexRuntimeUnavailable, match="ChatGPT-managed authentication"):
        invoke_structured(
            tmp_path,
            role="qa",
            prompt="Review.",
            images=[],
            output_schema=OUTPUT_SCHEMA,
            timeout=5,
            command=_fake_server(tmp_path, account_type="apiKey"),
        )


def test_role_effort_override_precedes_the_global_default(monkeypatch) -> None:
    monkeypatch.setenv("EDITABLE_PPT_CODEX_EFFORT", "low")
    monkeypatch.setenv("EDITABLE_PPT_VISUAL_MATERIAL_SEARCH_CODEX_EFFORT", "high")

    assert _effort_override("visual-material-search") == "high"
    assert _effort_override("qa") == "low"


def test_invalid_effort_is_rejected_before_starting_a_model_turn(monkeypatch) -> None:
    monkeypatch.setenv("EDITABLE_PPT_QA_CODEX_EFFORT", "extreme")

    with pytest.raises(ValueError, match="Codex effort"):
        _effort_override("qa")


def test_effort_override_is_recorded_in_the_safe_subscription_trace(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("EDITABLE_PPT_QA_CODEX_EFFORT", "high")

    result = invoke_structured(
        tmp_path,
        role="qa",
        prompt="Review.",
        images=[],
        output_schema=OUTPUT_SCHEMA,
        timeout=5,
        command=_fake_server(tmp_path),
    )

    assert result.effort == "high"
    assert result.safe_trace["effort"] == "high"


def test_malformed_structured_output_is_retryable(tmp_path: Path) -> None:
    with pytest.raises(CodexRuntimeUnavailable, match="structured output"):
        invoke_structured(
            tmp_path,
            role="reconstruction",
            prompt="Reconstruct.",
            images=[],
            output_schema=OUTPUT_SCHEMA,
            timeout=5,
            command=_fake_server(tmp_path, mode="malformed"),
        )
    stats = app_server_pool_stats()
    assert stats["process_starts"] == 1
    assert stats["evictions"] == 0
    assert stats["recoveries"] == 0


def test_timeout_terminates_the_app_server(tmp_path: Path) -> None:
    with pytest.raises(CodexRuntimeUnavailable, match="timeout"):
        invoke_structured(
            tmp_path,
            role="qa",
            prompt="Review.",
            images=[],
            output_schema=OUTPUT_SCHEMA,
            timeout=0.2,
            command=_fake_server(tmp_path, mode="timeout"),
        )


def test_qa_gateway_uses_subscription_runtime_without_api_key(tmp_path: Path, monkeypatch) -> None:
    project = _project(tmp_path, 1)
    request = workflow_state.next_action(project)["requests"][0]
    attempt = request["attempt"]
    claimed = workflow_state.dispatch(project, 1, "qa-worker", attempt)
    body = Path(claimed["generation_request"]["output"])
    receipt = write_valid_generation_receipt(project, 1, attempt, body)
    workflow_state.record_generation(
        project, 1, "qa-worker", attempt, body, generation_receipt=receipt,
    )
    action = workflow_state.next_action(project)
    work_path = project / action["qa_work_items"][0]["path"]
    work = json.loads(work_path.read_text(encoding="utf-8"))
    decision = {
        "status": "complete",
        "checks": {
            check: {"result": "pass", "detail": "Checked with Codex."}
            for check in v4_qa_gateway.CHECK_IDS
        },
        "required_image_presence": [],
        "required_directive_results": [],
    }
    captured = {}

    def fake_subscription(project_arg, **kwargs):
        captured.update(kwargs)
        assert project_arg == project
        return CodexStructuredResult(
            value=decision, thread_id="thr_qa", turn_id="turn_qa", model="gpt-test",
            model_provider="openai", auth_mode="chatgpt", plan_type="plus", usage={},
            safe_trace={"runtime": "codex-app-server"},
        )

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr(v4_qa_gateway, "_invoke_structured", fake_subscription)
    bundle_path = v4_qa_gateway.invoke_builtin_gateway(project, work_path, timeout=2)

    assert captured["role"] == "qa"
    assert captured["images"] == [project / work["body_image"]["path"]]
    bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
    assert bundle["attestation"]["provider"] == "codex-chatgpt"
    assert bundle["attestation"]["endpoint"] == "codex-app-server"
    assert bundle["attestation"]["service_request_id"] == "turn_qa"
