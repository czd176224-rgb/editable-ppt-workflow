from __future__ import annotations

import hashlib
import hmac
import io
import json
import os
import shutil
import sys
import threading
import time
from pathlib import Path

import pytest
from PIL import Image


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from codex_subscription_runtime import (  # noqa: E402
    CodexRuntimeUnavailable,
    CodexStructuredResult,
    invoke_structured,
)
import codex_subscription_runtime  # noqa: E402
from codex_web_material_gateway import (  # noqa: E402
    DownloadResponse,
    SearchMaterialBlocked,
    download_visual_material,
    search_visual_material,
    search_visual_materials,
    verify_search_material,
    verify_signed_batch_receipt,
    verify_signed_invocation_bundle,
)
from natural_comment_resolver import search_material_id  # noqa: E402
import page_material_bundle_v4  # noqa: E402
import codex_web_material_gateway  # noqa: E402
from test_page_material_bundle_v4 import _build, _contract, _project  # noqa: E402


def _slow_isolated_operation(delay: float):
    time.sleep(delay)
    return "late"


_BLOCKED_WRITE_PROGRAM = r"""
import os, sys, time
payload = sys.stdin.buffer.read()
flags = os.O_WRONLY | os.O_TRUNC | getattr(os, "O_BINARY", 0)
descriptor = os.open(sys.argv[1], flags, 0o600)
try:
    time.sleep(0.35)
    os.write(descriptor, payload)
    os.fsync(descriptor)
finally:
    os.close(descriptor)
"""


_BLOCKED_FSYNC_PROGRAM = r"""
import os, sys, time
payload = sys.stdin.buffer.read()
flags = os.O_WRONLY | os.O_TRUNC | getattr(os, "O_BINARY", 0)
descriptor = os.open(sys.argv[1], flags, 0o600)
try:
    os.write(descriptor, payload)
    time.sleep(0.35)
    os.fsync(descriptor)
finally:
    os.close(descriptor)
"""


def _png(width: int = 32, height: int = 24) -> bytes:
    stream = io.BytesIO()
    Image.new("RGB", (width, height), (18, 52, 86)).save(stream, format="PNG")
    return stream.getvalue()


class FakeTransport:
    def __init__(self, responses, *, addresses=None, resolve_sequences=None):
        self.responses = {
            url: list(value) if isinstance(value, list) else [value]
            for url, value in responses.items()
        }
        self.addresses = addresses or {}
        self.resolve_sequences = {
            host: list(sequence) for host, sequence in (resolve_sequences or {}).items()
        }
        self.requests = []

    def resolve(self, hostname: str, port: int):
        sequence = self.resolve_sequences.get(hostname)
        if sequence:
            return sequence.pop(0)
        return self.addresses.get(hostname, ["93.184.216.34"])

    def get(self, url: str, *, connect_ip: str, timeout: float, max_bytes: int):
        self.requests.append((url, connect_ip, timeout, max_bytes))
        response = self.responses[url].pop(0)
        return response


def _candidate(**changes):
    value = {
        "source_page_url": "https://news.example/story",
        "direct_image_url": "https://cdn.example/photo.png",
        "title": "Operation Phoenix press briefing",
        "publisher": "Example Newsroom",
        "caption": "Executives at the public press briefing.",
        "matched_entities": ["Operation Phoenix"],
        "retrieved_at": "2026-08-01T12:30:00Z",
    }
    value.update(changes)
    return value


def _directive(*, required: bool = True):
    query = "Operation Phoenix official press photo"
    material_id = search_material_id(query)
    return {
        "directive_id": "directive-abc123",
        "required": required,
        "search_required": True,
        "search_query": query,
        "decisions": [
            {
                "target": "material.search_evidence",
                "action": "require",
                "material_id": material_id,
            }
        ],
    }


def _result(candidates, *, safe_trace=None):
    usage = {"inputTokens": 10, "outputTokens": 20}
    trace = safe_trace or {
        "runtime": "codex-app-server",
        "role": "visual-material-search",
        "thread_id": "thr-search",
        "turn_id": "turn-search",
        "model": "gpt-test",
        "model_provider": "openai",
        "auth_mode": "chatgpt",
        "plan_type": "plus",
        "usage": usage,
        "image_count": 0,
        "web_search": "live",
    }
    return CodexStructuredResult(
        value={"candidates": candidates},
        thread_id="thr-search",
        turn_id="turn-search",
        model="gpt-test",
        model_provider="openai",
        auth_mode="chatgpt",
        plan_type="plus",
        usage=usage,
        safe_trace=trace,
    )


def _batch_directive(index: int, *, entity: str | None = None):
    entity = entity or f"Enterprise {index}"
    query = f"{entity} official logo"
    material_id = search_material_id(query)
    return {
        "directive_id": f"directive-{index}",
        "parent_directive_id": "parent-directive",
        "entity": entity,
        "material_role": "enterprise_logo",
        "required": True,
        "search_required": True,
        "search_query": query,
        "max_results": 1,
        "decisions": [{
            "target": "material.search_evidence", "action": "require",
            "material_id": material_id,
        }],
    }


def _batch_result(results):
    single = _result([])
    return CodexStructuredResult(
        value={"results": results}, thread_id=single.thread_id, turn_id=single.turn_id,
        model=single.model, model_provider=single.model_provider,
        auth_mode=single.auth_mode, plan_type=single.plan_type,
        usage=single.usage, safe_trace=single.safe_trace,
    )


def _batch_item(directive, *, candidate=None):
    return {
        "material_id": directive["decisions"][0]["material_id"],
        "directive_id": directive["directive_id"],
        "entity": directive["entity"],
        "candidates": [candidate] if candidate is not None else [],
    }


def test_incomplete_app_server_safe_trace_fails_before_search_commit(tmp_path: Path) -> None:
    calls = 0

    def invoke(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return _result(
            [_candidate()],
            safe_trace={"runtime": "codex-app-server", "auth_mode": "chatgpt"},
        )

    with pytest.raises(SearchMaterialBlocked, match="safe trace|trace"):
        search_visual_material(
            tmp_path,
            directive=_directive(required=True),
            page_context={"page_number": 1, "body_text": "Locked Word fact."},
            timeout=2,
            invoke=invoke,
            transport=FakeTransport({
                "https://cdn.example/photo.png": DownloadResponse(
                    200, {"content-type": "image/png"}, _png(),
                ),
            }),
        )

    assert calls == 1
    assert not list(tmp_path.rglob("search-cache-*.json"))


def test_live_search_uses_app_server_config_and_search_role(tmp_path: Path) -> None:
    captured = {}

    def fake_invoke(project, **kwargs):
        captured.update(kwargs)
        return _result([])

    with pytest.warns(RuntimeWarning, match="advisory visual material"):
        search_visual_material(
            tmp_path,
            directive=_directive(required=False),
            page_context={"page_number": 7, "body_text": "Locked Word fact."},
            timeout=2,
            invoke=fake_invoke,
            transport=FakeTransport({}),
        )

    assert captured["role"] == "visual-material-search"
    assert captured["web_search"] == "live"
    assert captured["images"] == []
    assert captured["output_schema"]["properties"]["candidates"]["maxItems"] == 3
    assert "Locked Word fact." in captured["prompt"]


def test_required_app_server_timeout_has_a_retryable_failure_code(tmp_path: Path) -> None:
    def timed_out(*_args, **_kwargs):
        raise CodexRuntimeUnavailable("Codex App Server timeout")

    with pytest.raises(SearchMaterialBlocked) as captured:
        search_visual_material(
            tmp_path,
            directive=_directive(required=True),
            page_context={"page_number": 1, "body_text": "Locked Word fact."},
            timeout=2,
            invoke=timed_out,
            transport=FakeTransport({}),
        )

    assert captured.value.code == "codex_app_server_timeout"


def test_single_search_cache_verification_maps_caller_deadline_to_cancellation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = [0.0]
    cache_path = tmp_path / "search-cache.json"
    cache_path.write_text("{}", encoding="utf-8")

    def cancelled_verify(*_args, **_kwargs):
        clock[0] = 2.0
        raise SearchMaterialBlocked("cache verification timed out")

    monkeypatch.setattr(codex_web_material_gateway.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(codex_web_material_gateway, "_search_cache_path", lambda *_a, **_k: cache_path)
    monkeypatch.setattr(codex_web_material_gateway, "_verify_cached_search", cancelled_verify)
    with pytest.raises(SearchMaterialBlocked) as captured:
        search_visual_material(
            tmp_path, directive=_directive(), page_context={"page_number": 1},
            timeout=10, deadline=1.0,
        )

    assert captured.value.code == "material_search_cancelled"
    assert captured.value.state == "cancelled"


def test_single_search_invocation_write_maps_caller_deadline_to_cancellation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = [0.0]

    def cancelled_write(*_args, **_kwargs):
        clock[0] = 2.0
        raise SearchMaterialBlocked("invocation write timed out")

    monkeypatch.setattr(codex_web_material_gateway.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(codex_web_material_gateway, "_write_invocation_bundle", cancelled_write)
    with pytest.raises(SearchMaterialBlocked) as captured:
        search_visual_material(
            tmp_path, directive=_directive(), page_context={"page_number": 1},
            timeout=10, deadline=1.0,
            invoke=lambda *_a, **_k: _result([_candidate()]),
        )

    assert captured.value.code == "material_search_cancelled"
    assert captured.value.state == "cancelled"


def test_single_search_final_commit_maps_caller_deadline_to_cancellation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = [0.0]

    def cancelled_commit(*_args, **_kwargs):
        clock[0] = 2.0
        raise SearchMaterialBlocked("cache commit timed out")

    monkeypatch.setattr(codex_web_material_gateway.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(codex_web_material_gateway, "_write_invocation_bundle", lambda *_a, **_k: "invocation.json")
    monkeypatch.setattr(codex_web_material_gateway, "_sha256_file", lambda *_a, **_k: "a" * 64)
    monkeypatch.setattr(
        codex_web_material_gateway, "_read_signed_invocation",
        lambda *_a, **_k: {
            "signature": "b" * 64, "sealed_sha256": "c" * 64,
        },
    )
    monkeypatch.setattr(
        codex_web_material_gateway, "download_visual_material",
        lambda *_a, **_k: [{"material_id": _directive()["decisions"][0]["material_id"]}],
    )
    monkeypatch.setattr(codex_web_material_gateway, "_write_search_cache", cancelled_commit)
    with pytest.raises(SearchMaterialBlocked) as captured:
        search_visual_material(
            tmp_path, directive=_directive(), page_context={"page_number": 1},
            timeout=10, deadline=1.0,
            invoke=lambda *_a, **_k: _result([_candidate()]),
        )

    assert captured.value.code == "material_search_cancelled"
    assert captured.value.state == "cancelled"


def _protocol_server(tmp_path: Path, account_type: str = "chatgpt"):
    capture = tmp_path / "protocol.json"
    script = tmp_path / "fake_protocol_server.py"
    script.write_text(
        """
import json, pathlib, sys
capture = pathlib.Path(sys.argv[1])
account_type = sys.argv[2]
seen = []
def send(value):
    print(json.dumps(value, separators=(",", ":")), flush=True)
for line in sys.stdin:
    request = json.loads(line); seen.append(request)
    method = request.get("method"); request_id = request.get("id")
    if method == "initialize": send({"id": request_id, "result": {}})
    elif method == "account/read":
        send({"id": request_id, "result": {"account": {"type": account_type, "planType": "plus"}}})
    elif method == "thread/start":
        capture.write_text(json.dumps(seen), encoding="utf-8")
        send({"id": request_id, "result": {"thread": {"id": "thr"}, "model": "gpt", "modelProvider": "openai"}})
    elif method == "turn/start":
        send({"id": request_id, "result": {"turn": {"id": "turn"}}})
        send({"method": "item/completed", "params": {"item": {"type": "agentMessage", "text": "{\\\"ok\\\":true}"}}})
        send({"method": "turn/completed", "params": {"turn": {"id": "turn", "error": None, "usage": {}}}})
""".strip()
        + "\n",
        encoding="utf-8",
    )
    return [sys.executable, str(script), str(capture), account_type], capture


def test_app_server_thread_enables_live_search_only_when_requested(tmp_path: Path) -> None:
    schema = {
        "type": "object",
        "additionalProperties": False,
        "required": ["ok"],
        "properties": {"ok": {"const": True}},
    }
    command, capture = _protocol_server(tmp_path)
    result = invoke_structured(
        tmp_path,
        role="visual-material-search",
        prompt="Search.",
        images=[],
        output_schema=schema,
        timeout=5,
        command=command,
        web_search="live",
        capability_probe=lambda _command, _timeout: frozenset(
            {"disabled", "cached", "indexed", "live"}
        ),
    )
    requests = json.loads(capture.read_text(encoding="utf-8"))
    thread = next(item for item in requests if item.get("method") == "thread/start")

    assert thread["params"]["config"]["web_search"] == "live"
    assert result.safe_trace["web_search"] == "live"


def test_live_search_still_rejects_api_key_authentication(tmp_path: Path) -> None:
    command, _capture = _protocol_server(tmp_path, "apiKey")
    with pytest.raises(CodexRuntimeUnavailable, match="ChatGPT-managed authentication"):
        invoke_structured(
            tmp_path,
            role="visual-material-search",
            prompt="Search.",
            images=[],
            output_schema={"type": "object"},
            timeout=5,
            command=command,
            web_search="live",
            capability_probe=lambda _command, _timeout: frozenset(
                {"disabled", "cached", "indexed", "live"}
            ),
        )


def test_non_material_roles_cannot_enable_live_search(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="reserved for visual material search"):
        invoke_structured(
            tmp_path,
            role="qa",
            prompt="Review.",
            images=[],
            output_schema={"type": "object"},
            timeout=5,
            web_search="live",
        )


@pytest.mark.parametrize(
    "url",
    [
        "http://cdn.example/photo.png",
        "file:///etc/passwd",
        "data:image/png;base64,AAAA",
        "https://user:secret@cdn.example/photo.png",
    ],
)
def test_downloader_rejects_non_https_and_credentialed_urls(tmp_path: Path, url: str) -> None:
    with pytest.raises(SearchMaterialBlocked, match="safe HTTPS"):
        download_visual_material(
            tmp_path,
            page_number=1,
            material_id=search_material_id("photo"),
            directive_id="directive-1",
            candidates=[_candidate(direct_image_url=url)],
            timeout=2,
            transport=FakeTransport({}),
        )


@pytest.mark.parametrize(
    "address",
    ["127.0.0.1", "::1", "10.0.0.4", "172.16.1.2", "192.168.0.8", "169.254.169.254", "224.0.0.1", "192.0.2.5"],
)
def test_downloader_rejects_non_public_destination_addresses(tmp_path: Path, address: str) -> None:
    transport = FakeTransport({}, addresses={"cdn.example": [address]})
    with pytest.raises(SearchMaterialBlocked, match="public Internet"):
        download_visual_material(
            tmp_path,
            page_number=1,
            material_id=search_material_id("photo"),
            directive_id="directive-1",
            candidates=[_candidate()],
            timeout=2,
            transport=transport,
        )


def test_downloader_rejects_dns_rebinding(tmp_path: Path) -> None:
    transport = FakeTransport(
        {"https://cdn.example/photo.png": DownloadResponse(200, {"content-type": "image/png"}, _png())},
        resolve_sequences={"cdn.example": [["93.184.216.34"], ["127.0.0.1"]]},
    )
    with pytest.raises(SearchMaterialBlocked, match="DNS"):
        download_visual_material(
            tmp_path,
            page_number=1,
            material_id=search_material_id("photo"),
            directive_id="directive-1",
            candidates=[_candidate()],
            timeout=2,
            transport=transport,
        )


def test_downloader_rejects_localhost_even_if_a_resolver_claims_it_is_public(tmp_path: Path) -> None:
    transport = FakeTransport(
        {"https://localhost/photo.png": DownloadResponse(200, {"content-type": "image/png"}, _png())}
    )
    with pytest.raises(SearchMaterialBlocked, match="public Internet"):
        download_visual_material(
            tmp_path,
            page_number=1,
            material_id=search_material_id("photo"),
            directive_id="directive-1",
            candidates=[_candidate(direct_image_url="https://localhost/photo.png")],
            timeout=2,
            transport=transport,
        )


def test_downloader_revalidates_redirect_destinations(tmp_path: Path) -> None:
    transport = FakeTransport(
        {
            "https://cdn.example/photo.png": DownloadResponse(302, {"location": "https://127.0.0.1/private.png"}, b""),
        },
        addresses={"cdn.example": ["93.184.216.34"]},
    )
    with pytest.raises(SearchMaterialBlocked, match="public Internet"):
        download_visual_material(
            tmp_path,
            page_number=1,
            material_id=search_material_id("photo"),
            directive_id="directive-1",
            candidates=[_candidate()],
            timeout=2,
            transport=transport,
        )


def test_downloader_gives_each_candidate_an_independent_timeout(tmp_path: Path, monkeypatch) -> None:
    clock = [0.0]
    observed_deadlines: list[float] = []
    payload = _png()

    def fake_fetch(url, *, deadline, **_kwargs):
        observed_deadlines.append(deadline)
        if len(observed_deadlines) == 1:
            clock[0] = 3.0
            raise SearchMaterialBlocked(
                "first candidate timed out", code="candidate_network_timeout",
            )
        return {
            "body": payload, "mime_type": "image/png", "final_url": url,
            "content_length": len(payload), "body_bytes": len(payload),
        }

    monkeypatch.setattr(codex_web_material_gateway.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(codex_web_material_gateway, "_fetch", fake_fetch)
    materials = download_visual_material(
        tmp_path,
        page_number=1,
        material_id=search_material_id("photo"),
        directive_id="directive-1",
        candidates=[
            _candidate(direct_image_url="https://cdn.example/first.png"),
            _candidate(direct_image_url="https://cdn.example/second.png"),
        ],
        timeout=2,
        transport=FakeTransport({}),
    )

    assert len(materials) == 1
    assert materials[0]["direct_image_url"] == "https://cdn.example/second.png"
    assert observed_deadlines == [2.0, 5.0]


@pytest.mark.parametrize("status", [403, 404])
def test_downloader_continues_after_unavailable_http_candidate(
    tmp_path: Path, status: int,
) -> None:
    first = "https://cdn.example/unavailable.png"
    second = "https://cdn.example/available.png"
    transport = FakeTransport({
        first: DownloadResponse(status, {"content-type": "text/plain"}, b"blocked"),
        second: DownloadResponse(200, {"content-type": "image/png"}, _png()),
    })

    materials = download_visual_material(
        tmp_path,
        page_number=1,
        material_id=search_material_id("photo"),
        directive_id="directive-1",
        candidates=[
            _candidate(direct_image_url=first),
            _candidate(direct_image_url=second),
        ],
        timeout=2,
        transport=transport,
    )

    assert [item["direct_image_url"] for item in materials] == [second]


@pytest.mark.parametrize("failure", ["unsafe_url", "source_provenance", "signature"])
def test_downloader_never_falls_through_integrity_or_safety_failure_to_later_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str,
) -> None:
    first = "https://cdn.example/first.png"
    second = "https://cdn.example/second.png"
    first_candidate = _candidate(direct_image_url=first)
    if failure == "unsafe_url":
        first_candidate["direct_image_url"] = "http://cdn.example/unsafe.png"
    elif failure == "source_provenance":
        first_candidate["publisher"] = ""
    else:
        real_key = codex_web_material_gateway._attestation_key
        calls = 0

        def fail_first_signature(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise SearchMaterialBlocked("injected signature failure")
            return real_key(*args, **kwargs)

        monkeypatch.setattr(codex_web_material_gateway, "_attestation_key", fail_first_signature)
    transport = FakeTransport({
        first: DownloadResponse(200, {"content-type": "image/png"}, _png()),
        second: DownloadResponse(200, {"content-type": "image/png"}, _png(33, 25)),
    })

    with pytest.raises(SearchMaterialBlocked):
        download_visual_material(
            tmp_path,
            page_number=1,
            material_id=search_material_id("photo"),
            directive_id="directive-1",
            candidates=[first_candidate, _candidate(direct_image_url=second)],
            timeout=2,
            transport=transport,
        )

    assert all(request[0] != second for request in transport.requests)


def test_downloader_decode_timeout_fails_closed_without_requesting_later_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = "https://cdn.example/decode-timeout.png"
    second = "https://cdn.example/must-not-run.png"
    transport = FakeTransport({
        first: DownloadResponse(200, {"content-type": "image/png"}, _png()),
        second: DownloadResponse(200, {"content-type": "image/png"}, _png(33, 25)),
    })
    monkeypatch.setattr(
        codex_web_material_gateway,
        "_decode_image",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            SearchMaterialBlocked("image decode timed out")
        ),
    )

    with pytest.raises(SearchMaterialBlocked, match="image decode timed out"):
        download_visual_material(
            tmp_path,
            page_number=1,
            material_id=search_material_id("photo"),
            directive_id="directive-1",
            candidates=[
                _candidate(direct_image_url=first),
                _candidate(direct_image_url=second),
            ],
            timeout=2,
            transport=transport,
        )

    assert [request[0] for request in transport.requests] == [first]


@pytest.mark.parametrize(
    "mime,payload",
    [
        ("text/html", b"<html><script>alert(1)</script></html>"),
        ("image/svg+xml", b"<svg><script>alert(1)</script></svg>"),
        ("image/png", b"<html>not actually an image</html>"),
    ],
)
def test_downloader_rejects_non_raster_or_disguised_payloads(
    tmp_path: Path, mime: str, payload: bytes
) -> None:
    transport = FakeTransport(
        {"https://cdn.example/photo.png": DownloadResponse(200, {"content-type": mime}, payload)}
    )
    with pytest.raises(SearchMaterialBlocked, match="image"):
        download_visual_material(
            tmp_path,
            page_number=1,
            material_id=search_material_id("photo"),
            directive_id="directive-1",
            candidates=[_candidate()],
            timeout=2,
            transport=transport,
        )


def test_downloader_rejects_candidates_without_complete_source_provenance(tmp_path: Path) -> None:
    transport = FakeTransport(
        {"https://cdn.example/photo.png": DownloadResponse(200, {"content-type": "image/png"}, _png())}
    )
    with pytest.raises(SearchMaterialBlocked, match="provenance"):
        download_visual_material(
            tmp_path,
            page_number=1,
            material_id=search_material_id("photo"),
            directive_id="directive-1",
            candidates=[_candidate(publisher="")],
            timeout=2,
            transport=transport,
        )


def test_downloader_rejects_oversized_file_and_pixel_dimensions(tmp_path: Path) -> None:
    oversized = FakeTransport(
        {
            "https://cdn.example/photo.png": DownloadResponse(
                200, {"content-type": "image/png", "content-length": str(12 * 1024 * 1024 + 1)}, b""
            )
        }
    )
    with pytest.raises(SearchMaterialBlocked, match="12 MiB"):
        download_visual_material(
            tmp_path,
            page_number=1,
            material_id=search_material_id("photo"),
            directive_id="directive-1",
            candidates=[_candidate()],
            timeout=2,
            transport=oversized,
        )

    huge_pixels = FakeTransport(
        {"https://cdn.example/photo.png": DownloadResponse(200, {"content-type": "image/png"}, _png(8000, 5001))}
    )
    with pytest.raises(SearchMaterialBlocked, match="40 megapixels"):
        download_visual_material(
            tmp_path,
            page_number=1,
            material_id=search_material_id("photo"),
            directive_id="directive-1",
            candidates=[_candidate()],
            timeout=2,
            transport=huge_pixels,
        )


def test_downloader_limits_each_page_to_three_selected_images(tmp_path: Path) -> None:
    candidates = [_candidate(direct_image_url=f"https://cdn.example/{index}.png") for index in range(4)]
    with pytest.raises(SearchMaterialBlocked, match="three"):
        download_visual_material(
            tmp_path,
            page_number=1,
            material_id=search_material_id("photo"),
            directive_id="directive-1",
            candidates=candidates,
            timeout=2,
            transport=FakeTransport({}),
        )


@pytest.mark.parametrize(
    "material_id",
    ["search-request-../../escape", "search-request-not-a-digest", "search-request-abc/def"],
)
def test_downloader_rejects_unsafe_material_ids(tmp_path: Path, material_id: str) -> None:
    with pytest.raises(ValueError, match="stable search material_id"):
        download_visual_material(
            tmp_path,
            page_number=1,
            material_id=material_id,
            directive_id="directive-1",
            candidates=[_candidate()],
            timeout=2,
            transport=FakeTransport({}),
        )


def test_search_material_preserves_stable_id_and_binds_file_and_provenance(tmp_path: Path) -> None:
    directive = _directive()
    material_id = directive["decisions"][0]["material_id"]
    payload = _png()
    transport = FakeTransport(
        {"https://cdn.example/photo.png": DownloadResponse(200, {"content-type": "image/png"}, payload)}
    )

    materials = search_visual_material(
        tmp_path,
        directive=directive,
        page_context={"page_number": 7, "body_text": "Locked Word fact."},
        timeout=2,
        invoke=lambda *_args, **_kwargs: _result([_candidate()]),
        transport=transport,
    )

    assert len(materials) == 1
    material = materials[0]
    assert material["material_id"] == material_id
    assert material["asset_id"] == material_id
    assert material["directive_id"] == "directive-abc123"
    assert material["sha256"] == hashlib.sha256(payload).hexdigest()
    assert material["evidence_id"] != material_id
    assert material["source_page_url"] == "https://news.example/story"
    assert material["direct_image_url"] == "https://cdn.example/photo.png"
    assert material["publisher"] == "Example Newsroom"
    assert material["media_type"] == "image/png"
    assert material["width"] == 32 and material["height"] == 24
    local = tmp_path / material["local_path"]
    assert local.is_file() and local.read_bytes() == payload
    assert local.resolve().is_relative_to(tmp_path.resolve())

    invocation = json.loads((tmp_path / material["invocation_bundle_path"]).read_text(encoding="utf-8"))
    assert invocation["material_id"] == material_id
    assert invocation["directive_id"] == "directive-abc123"
    assert invocation["turn_id"] == "turn-search"
    assert invocation["model_response"]["candidates"][0]["title"] == material["title"]
    signature = invocation.pop("signature")
    assert len(signature) == 64
    seal = invocation.pop("sealed_sha256")
    canonical = json.dumps(invocation, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    assert seal == hashlib.sha256(canonical).hexdigest()
    assert verify_signed_invocation_bundle(tmp_path, tmp_path / material["invocation_bundle_path"])
    assert material["invocation_bundle_sha256"] == hashlib.sha256(
        (tmp_path / material["invocation_bundle_path"]).read_bytes()
    ).hexdigest()
    record_path = tmp_path / material["material_attestation_path"]
    record = json.loads(record_path.read_text(encoding="utf-8"))
    attestation = record["attestation"]
    assert record["attestation_sha256"] == hashlib.sha256(
        json.dumps(attestation, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    assert attestation["evidence_id"] == material["evidence_id"]
    assert attestation["image"]["sha256"] == material["sha256"]
    assert attestation["source"]["publisher"] == "Example Newsroom"
    assert attestation["invocation"]["sha256"] == material["invocation_bundle_sha256"]
    assert material["material_attestation_sha256"] == hashlib.sha256(
        record_path.read_bytes()
    ).hexdigest()
    assert verify_search_material(
        tmp_path,
        material,
        expected_material_id=material_id,
        expected_directive_id="directive-abc123",
        expected_query=directive["search_query"],
        deadline=time.monotonic() + 5,
    )["local_path"] == material["local_path"]
    invocation_path = tmp_path / material["invocation_bundle_path"]
    tampered = json.loads(invocation_path.read_text(encoding="utf-8"))
    tampered["model_response"]["candidates"][0]["title"] = "tampered"
    invocation_path.write_text(json.dumps(tampered), encoding="utf-8")
    assert not verify_signed_invocation_bundle(tmp_path, invocation_path)
    with pytest.raises(SearchMaterialBlocked, match="invocation"):
        verify_search_material(
            tmp_path,
            material,
            expected_material_id=material_id,
            expected_directive_id="directive-abc123",
            expected_query=directive["search_query"],
            deadline=time.monotonic() + 5,
        )


def test_plural_search_accepts_unordered_bijection_and_replays_each_signed_cache(tmp_path: Path) -> None:
    directives = [_batch_directive(1), _batch_directive(2)]
    payloads = [_png(31, 21), _png(32, 22)]
    candidates = [
        _candidate(
            direct_image_url=f"https://cdn.example/logo-{index}.png",
            matched_entities=[directive["entity"]], title=f"Logo {index}",
        )
        for index, directive in enumerate(directives, 1)
    ]
    calls = 0

    def invoke(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return _batch_result([
            _batch_item(directives[1], candidate=candidates[1]),
            _batch_item(directives[0], candidate=candidates[0]),
        ])

    transport = FakeTransport({
        candidate["direct_image_url"]: DownloadResponse(
            200, {"content-type": "image/png"}, payload,
        )
        for candidate, payload in zip(candidates, payloads, strict=True)
    })
    context = {"page_number": 8, "body_text": "Locked companies"}
    first = search_visual_materials(
        tmp_path, directives=directives, page_context=context, timeout=10,
        invoke=invoke, transport=transport,
    )
    replay = search_visual_materials(
        tmp_path, directives=directives, page_context=context, timeout=10,
        invoke=lambda *_args, **_kwargs: pytest.fail("signed item cache must replay"),
        transport=FakeTransport({}),
    )

    assert calls == 1
    assert [items[0]["directive_id"] for items in first] == ["directive-1", "directive-2"]
    assert [items[0]["sha256"] for items in replay] == [items[0]["sha256"] for items in first]
    assert [items[0]["batch_receipt_path"] for items in replay] == [
        items[0]["batch_receipt_path"] for items in first
    ]
    receipts = {items[0]["batch_receipt_path"] for items in first}
    assert len(receipts) == 1
    receipt_path = tmp_path / receipts.pop()
    assert verify_signed_batch_receipt(tmp_path, receipt_path)
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert receipt["request"]["requests"][0]["directive_id"] == "directive-1"
    assert receipt["response"]["value"]["results"][0]["directive_id"] == "directive-2"
    for directive, items in zip(directives, first, strict=True):
        item = items[0]
        assert "invocation_bundle_path" not in item
        record = json.loads((tmp_path / item["material_attestation_path"]).read_text(encoding="utf-8"))
        assert "invocation" not in record["attestation"]
        binding = record["attestation"]["batch_receipt"]["binding"]
        assert binding == {
            "material_id": directive["decisions"][0]["material_id"],
            "directive_id": directive["directive_id"],
            "entity": directive["entity"],
        }
        assert record["attestation"]["batch_receipt"]["request_identity"] == {
            "material_id": directive["decisions"][0]["material_id"],
            "directive_id": directive["directive_id"],
            "entity": directive["entity"],
            "query": directive["search_query"],
            "required": directive["required"],
            "material_role": directive["material_role"],
            "max_results": directive["max_results"],
        }


def test_plural_search_tries_later_candidates_within_the_same_request_before_blocking(
    tmp_path: Path,
) -> None:
    directives = [_batch_directive(index) for index in range(1, 4)]
    for directive in directives:
        directive["max_results"] = 3
    unavailable = _candidate(
        direct_image_url="https://cdn.example/first-403.png",
        matched_entities=[directives[0]["entity"]],
    )
    fallback = _candidate(
        direct_image_url="https://cdn.example/fallback.png",
        matched_entities=[directives[0]["entity"]],
    )
    other_candidates = {
        directive["directive_id"]: _candidate(
            direct_image_url=f"https://cdn.example/{directive['directive_id']}.png",
            matched_entities=[directive["entity"]],
        )
        for directive in directives[1:]
    }

    def invoke(*_args, **_kwargs):
        return _batch_result([
            {
                **_batch_item(directives[0]),
                "candidates": [unavailable, fallback],
            },
            *[
                _batch_item(directive, candidate=other_candidates[directive["directive_id"]])
                for directive in directives[1:]
            ],
        ])

    transport = FakeTransport({
        unavailable["direct_image_url"]: DownloadResponse(
            403, {"content-type": "text/plain"}, b"forbidden",
        ),
        fallback["direct_image_url"]: DownloadResponse(
            200, {"content-type": "image/png"}, _png(31, 21),
        ),
        **{
            candidate["direct_image_url"]: DownloadResponse(
                200, {"content-type": "image/png"}, _png(32 + index, 22),
            )
            for index, candidate in enumerate(other_candidates.values())
        },
    })

    outcomes = search_visual_materials(
        tmp_path,
        directives=directives,
        page_context={"page_number": 25, "body_text": "Locked"},
        timeout=5,
        invoke=invoke,
        transport=transport,
    )

    assert outcomes[0][0]["direct_image_url"] == fallback["direct_image_url"]
    assert all(len(items) == 1 for items in outcomes)


@pytest.mark.parametrize("mutation", ["missing", "duplicate", "extra", "wrong_entity"])
def test_plural_search_rejects_non_bijective_batch_response(tmp_path: Path, mutation: str) -> None:
    directives = [_batch_directive(1), _batch_directive(2)]
    results = [_batch_item(item, candidate=_candidate(matched_entities=[item["entity"]])) for item in directives]
    if mutation == "missing":
        results.pop()
    elif mutation == "duplicate":
        results[1] = dict(results[0])
    elif mutation == "extra":
        results.append(_batch_item(_batch_directive(3), candidate=_candidate()))
    else:
        results[1] = {**results[1], "entity": "Wrong Enterprise"}

    with pytest.raises(SearchMaterialBlocked, match="bijection|binding"):
        search_visual_materials(
            tmp_path, directives=directives,
            page_context={"page_number": 9, "body_text": "Locked"}, timeout=5,
            invoke=lambda *_args, **_kwargs: _batch_result(results),
            transport=FakeTransport({}),
        )
    assert not list(tmp_path.rglob("search-cache-*.json"))


def test_plural_search_shards_at_five_requests(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    directives = [_batch_directive(index) for index in range(1, 7)]
    shard_sizes: list[int] = []
    active = 0
    peak = 0
    guard = threading.Lock()
    both_started = threading.Event()

    def invoke(*_args, **kwargs):
        nonlocal active, peak
        marker = "SEARCH_REQUESTS: "
        requests = json.loads(kwargs["prompt"].split(marker, 1)[1].split("\n", 1)[0])
        with guard:
            shard_sizes.append(len(requests))
            active += 1
            peak = max(peak, active)
            if active == 2:
                both_started.set()
        both_started.wait(1.0)
        by_directive = {item["directive_id"]: item for item in directives}
        try:
            return _batch_result([
                _batch_item(by_directive[item["directive_id"]], candidate=_candidate(
                    direct_image_url=f"https://cdn.example/{item['directive_id']}.png",
                    matched_entities=[item["entity"]],
                ))
                for item in requests
            ])
        finally:
            with guard:
                active -= 1

    def fake_download(_project, **kwargs):
        return [{
            "material_id": kwargs["material_id"], "asset_id": kwargs["material_id"],
            "directive_id": kwargs["directive_id"], "query": kwargs["query"],
        }]

    monkeypatch.setattr(codex_web_material_gateway, "download_visual_material", fake_download)
    monkeypatch.setattr(codex_web_material_gateway, "_write_search_cache", lambda *_args, **_kwargs: None)
    outcomes = search_visual_materials(
        tmp_path, directives=directives,
        page_context={"page_number": 10, "body_text": "Locked"}, timeout=5,
        invoke=invoke, transport=FakeTransport({}),
    )

    assert sorted(shard_sizes) == [1, 5]
    assert peak == 2
    assert len(outcomes) == 6


def test_plural_search_handles_twenty_requests_as_four_bounded_shards(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    directives = [_batch_directive(index) for index in range(1, 21)]
    shard_sizes: list[int] = []
    active = 0
    peak = 0
    guard = threading.Lock()

    def invoke(*_args, **kwargs):
        nonlocal active, peak
        marker = "SEARCH_REQUESTS: "
        requests = json.loads(kwargs["prompt"].split(marker, 1)[1].split("\n", 1)[0])
        with guard:
            shard_sizes.append(len(requests))
            active += 1
            peak = max(peak, active)
        try:
            time.sleep(0.03)
            by_id = {item["directive_id"]: item for item in directives}
            return _batch_result([
                _batch_item(by_id[item["directive_id"]], candidate=_candidate(
                    matched_entities=[item["entity"]],
                ))
                for item in requests
            ])
        finally:
            with guard:
                active -= 1

    monkeypatch.setattr(codex_web_material_gateway, "download_visual_material", lambda *_a, **_k: [{}])
    monkeypatch.setattr(codex_web_material_gateway, "_write_search_cache", lambda *_a, **_k: None)
    outcomes = search_visual_materials(
        tmp_path, directives=directives,
        page_context={"page_number": 20, "body_text": "Locked"}, timeout=2,
        invoke=invoke, transport=FakeTransport({}),
    )

    assert sorted(shard_sizes) == [5, 5, 5, 5]
    assert peak == 2
    assert len(outcomes) == 20


def test_plural_search_rejects_more_than_twenty_requests(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="cannot exceed 20"):
        search_visual_materials(
            tmp_path,
            directives=[_batch_directive(index) for index in range(1, 22)],
            page_context={"page_number": 21, "body_text": "Locked"},
            timeout=2,
        )


def test_plural_search_limits_download_concurrency_to_three(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    directives = [_batch_directive(index) for index in range(1, 6)]
    active = 0
    peak = 0
    guard = threading.Lock()

    def invoke(*_args, **_kwargs):
        return _batch_result([
            _batch_item(item, candidate=_candidate(matched_entities=[item["entity"]]))
            for item in directives
        ])

    def fake_download(_project, **kwargs):
        nonlocal active, peak
        with guard:
            active += 1
            peak = max(peak, active)
        time.sleep(0.08)
        with guard:
            active -= 1
        return [{
            "material_id": kwargs["material_id"], "asset_id": kwargs["material_id"],
            "directive_id": kwargs["directive_id"], "query": kwargs["query"],
        }]

    monkeypatch.setattr(codex_web_material_gateway, "download_visual_material", fake_download)
    monkeypatch.setattr(codex_web_material_gateway, "_write_search_cache", lambda *_args, **_kwargs: None)
    search_visual_materials(
        tmp_path, directives=directives,
        page_context={"page_number": 11, "body_text": "Locked"}, timeout=5,
        invoke=invoke, transport=FakeTransport({}),
    )

    assert peak == 3


def test_plural_search_does_not_apply_a_page_deadline_across_shards(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    directives = [_batch_directive(index) for index in range(1, 7)]
    clock = [0.0]
    observed_timeouts: list[float] = []

    def invoke(*_args, **kwargs):
        marker = "SEARCH_REQUESTS: "
        requests = json.loads(kwargs["prompt"].split(marker, 1)[1].split("\n", 1)[0])
        observed_timeouts.append(kwargs["timeout"])
        clock[0] += 310.0
        by_id = {item["directive_id"]: item for item in directives}
        return _batch_result([
            _batch_item(by_id[item["directive_id"]], candidate=_candidate(
                matched_entities=[item["entity"]],
            ))
            for item in requests
        ])

    monkeypatch.setattr(codex_web_material_gateway.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(
        codex_web_material_gateway, "_write_batch_receipt",
        lambda *_a, **_k: {
            "path": "receipt.json", "sha256": "a" * 64,
            "sealed_sha256": "b" * 64, "signature": "c" * 64,
            "batch_id": "batch-test",
        },
    )
    monkeypatch.setattr(codex_web_material_gateway, "download_visual_material", lambda *_a, **_k: [{}])
    monkeypatch.setattr(codex_web_material_gateway, "_write_search_cache", lambda *_a, **_k: None)
    search_visual_materials(
        tmp_path, directives=directives,
        page_context={"page_number": 19, "body_text": "Locked"}, timeout=2,
        max_search_concurrency=1,
        invoke=invoke, transport=FakeTransport({}),
    )

    assert len(observed_timeouts) == 2
    assert observed_timeouts == [2, 2]


def test_plural_search_caller_cancel_deadline_stops_queued_shards(tmp_path: Path, monkeypatch) -> None:
    directives = [_batch_directive(index) for index in range(1, 12)]
    clock = [0.0]
    calls = 0

    def invoke(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        clock[0] = 3.0
        raise CodexRuntimeUnavailable("Codex App Server timeout")

    monkeypatch.setattr(codex_web_material_gateway.time, "monotonic", lambda: clock[0])
    with pytest.raises(SearchMaterialBlocked) as captured:
        search_visual_materials(
            tmp_path, directives=directives,
            page_context={"page_number": 24, "body_text": "Locked"}, timeout=2,
            deadline=2.5, max_search_concurrency=1,
            invoke=invoke, transport=FakeTransport({}),
        )

    assert captured.value.code == "material_search_cancelled"
    assert captured.value.state == "cancelled"
    assert calls == 1


def test_plural_search_commits_successful_item_before_failed_item_retry(tmp_path: Path) -> None:
    directives = [_batch_directive(1), _batch_directive(2)]
    candidates = {
        item["directive_id"]: _candidate(
            direct_image_url=f"https://cdn.example/{item['directive_id']}.png",
            matched_entities=[item["entity"]],
        )
        for item in directives
    }
    request_history: list[list[str]] = []

    def invoke(*_args, **kwargs):
        marker = "SEARCH_REQUESTS: "
        requests = json.loads(kwargs["prompt"].split(marker, 1)[1].split("\n", 1)[0])
        request_history.append([item["directive_id"] for item in requests])
        by_id = {item["directive_id"]: item for item in directives}
        return _batch_result([
            _batch_item(by_id[item["directive_id"]], candidate=candidates[item["directive_id"]])
            for item in requests
        ])

    context = {"page_number": 12, "body_text": "Locked"}
    with pytest.raises(SearchMaterialBlocked):
        search_visual_materials(
            tmp_path, directives=directives, page_context=context, timeout=10,
            invoke=invoke,
            transport=FakeTransport({
                candidates["directive-1"]["direct_image_url"]: DownloadResponse(
                    200, {"content-type": "image/png"}, _png(30, 20),
                ),
                candidates["directive-2"]["direct_image_url"]: DownloadResponse(
                    200, {"content-type": "image/png"}, b"not-an-image",
                ),
            }),
        )
    assert len(list(tmp_path.rglob("search-cache-*.json"))) == 1

    completed = search_visual_materials(
        tmp_path, directives=directives, page_context=context, timeout=10,
        invoke=invoke,
        transport=FakeTransport({
            candidates["directive-2"]["direct_image_url"]: DownloadResponse(
                200, {"content-type": "image/png"}, _png(31, 21),
            ),
        }),
    )

    assert request_history == [["directive-1", "directive-2"], ["directive-2"]]
    assert all(len(items) == 1 for items in completed)
    assert len(list(tmp_path.rglob("search-cache-*.json"))) == 2


def test_plural_search_does_not_commit_duplicate_image_for_two_materials(tmp_path: Path) -> None:
    directives = [_batch_directive(1), _batch_directive(2)]
    request_history: list[list[str]] = []

    def invoke(*_args, **kwargs):
        marker = "SEARCH_REQUESTS: "
        requests = json.loads(kwargs["prompt"].split(marker, 1)[1].split("\n", 1)[0])
        request_history.append([item["directive_id"] for item in requests])
        by_id = {item["directive_id"]: item for item in directives}
        suffix = "first" if len(request_history) == 1 else "retry"
        return _batch_result([
            _batch_item(by_id[item["directive_id"]], candidate=_candidate(
                direct_image_url=f"https://cdn.example/{suffix}-{item['directive_id']}.png",
                matched_entities=[item["entity"]],
            ))
            for item in requests
        ])

    duplicate = _png(30, 20)
    transport = FakeTransport({
        "https://cdn.example/first-directive-1.png": DownloadResponse(
            200, {"content-type": "image/png"}, duplicate,
        ),
        "https://cdn.example/first-directive-2.png": DownloadResponse(
            200, {"content-type": "image/png"}, duplicate,
        ),
        "https://cdn.example/retry-directive-1.png": DownloadResponse(
            200, {"content-type": "image/png"}, _png(31, 20),
        ),
        "https://cdn.example/retry-directive-2.png": DownloadResponse(
            200, {"content-type": "image/png"}, _png(32, 20),
        ),
    })
    context = {"page_number": 22, "body_text": "Locked"}

    with pytest.raises(SearchMaterialBlocked) as captured:
        search_visual_materials(
            tmp_path, directives=directives, page_context=context, timeout=10,
            invoke=invoke, transport=transport,
        )
    assert captured.value.code == "required_search_material_duplicate"
    assert len(list(tmp_path.rglob("search-cache-*.json"))) == 1

    outcomes = search_visual_materials(
        tmp_path, directives=directives, page_context=context, timeout=10,
        invoke=invoke, transport=transport,
    )
    assert len(request_history[1]) == 1
    assert all(len(items) == 1 for items in outcomes)


def test_plural_search_preserves_app_server_timeout_code(tmp_path: Path) -> None:
    with pytest.raises(SearchMaterialBlocked) as captured:
        search_visual_materials(
            tmp_path,
            directives=[_batch_directive(1)],
            page_context={"page_number": 13, "body_text": "Locked"},
            timeout=5,
            invoke=lambda *_args, **_kwargs: (_ for _ in ()).throw(
                CodexRuntimeUnavailable("Codex App Server timeout")
            ),
        )

    assert captured.value.code == "codex_app_server_timeout"


def test_plural_search_required_empty_result_has_stable_code(tmp_path: Path) -> None:
    directive = _batch_directive(1)
    with pytest.raises(SearchMaterialBlocked) as captured:
        search_visual_materials(
            tmp_path,
            directives=[directive],
            page_context={"page_number": 14, "body_text": "Locked"},
            timeout=5,
            invoke=lambda *_args, **_kwargs: _batch_result([_batch_item(directive)]),
            transport=FakeTransport({}),
        )

    assert captured.value.code == "required_search_material_empty"


def test_plural_search_reports_failure_by_request_order_not_completion_order(tmp_path: Path) -> None:
    directives = [_batch_directive(index) for index in range(1, 7)]

    def invoke(*_args, **kwargs):
        marker = "SEARCH_REQUESTS: "
        requests = json.loads(kwargs["prompt"].split(marker, 1)[1].split("\n", 1)[0])
        if requests[0]["directive_id"] == "directive-6":
            raise CodexRuntimeUnavailable("Codex App Server timeout")
        time.sleep(0.05)
        by_id = {item["directive_id"]: item for item in directives}
        return _batch_result([_batch_item(by_id[item["directive_id"]]) for item in requests])

    with pytest.raises(SearchMaterialBlocked) as captured:
        search_visual_materials(
            tmp_path, directives=directives,
            page_context={"page_number": 23, "body_text": "Locked"}, timeout=2,
            invoke=invoke, transport=FakeTransport({}),
        )

    assert captured.value.code == "required_search_material_empty"


@pytest.mark.parametrize(
    ("field", "changed"),
    [
        ("entity", "Another Enterprise"),
        ("material_role", "press_photo"),
        ("max_results", 2),
    ],
)
def test_plural_search_cache_identity_binds_batch_request_fields(
    tmp_path: Path, field: str, changed: object,
) -> None:
    request = codex_web_material_gateway._batch_request_identity(_batch_directive(1))
    altered = {**request, field: changed}
    context = {"page_number": 15, "body_text": "Locked"}
    budget = codex_web_material_gateway.ResourceBudget()
    first = codex_web_material_gateway._batch_search_cache_identity(
        page_context=context, request=request, budget=budget,
    )
    second = codex_web_material_gateway._batch_search_cache_identity(
        page_context=context, request=altered, budget=budget,
    )

    assert first != second
    assert codex_web_material_gateway._search_cache_path(
        tmp_path, first, deadline=time.monotonic() + 5,
    ) != codex_web_material_gateway._search_cache_path(
        tmp_path, second, deadline=time.monotonic() + 5,
    )


def test_committed_search_artifact_resumes_twice_without_duplicate_app_server_call(tmp_path: Path) -> None:
    """A crash after search commit but before bundle assembly must replay signed local evidence."""
    directive = _directive()
    payload = _png()
    calls = 0

    def invoke(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return _result([_candidate()])

    context = {
        "page_number": 4,
        "source_hash": "a" * 64,
        "body_text": "Locked Word fact.",
        "page_comments": [{
            "comment_id": "news", "text": directive["search_query"],
            "author": "reviewer", "timestamp": None,
        }],
    }
    first = search_visual_material(
        tmp_path, directive=directive, page_context=context, timeout=10,
        invoke=invoke,
        transport=FakeTransport({
            "https://cdn.example/photo.png": DownloadResponse(
                200, {"content-type": "image/png"}, payload,
            ),
        }),
    )
    second = search_visual_material(
        tmp_path, directive=directive, page_context=context, timeout=10, invoke=invoke,
        transport=FakeTransport({}),
    )
    third = search_visual_material(
        tmp_path, directive=directive, page_context=context, timeout=10, invoke=invoke,
        transport=FakeTransport({}),
    )

    assert calls == 1
    assert second == third
    assert second[0]["sha256"] == first[0]["sha256"]
    assert second[0]["local_path"] == first[0]["local_path"]
    assert second[0]["material_attestation_sha256"] == first[0]["material_attestation_sha256"]
    assert verify_search_material(
        tmp_path, second[0],
        expected_material_id=directive["decisions"][0]["material_id"],
        expected_directive_id=directive["directive_id"],
        expected_query=directive["search_query"],
        deadline=time.monotonic() + 5,
    )["sha256"] == hashlib.sha256(payload).hexdigest()


def test_changed_search_page_authority_gets_a_distinct_live_call(tmp_path: Path) -> None:
    directive = _directive()
    payload = _png()
    calls = 0

    def invoke(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return _result([_candidate()])

    def run(source_hash: str):
        return search_visual_material(
            tmp_path,
            directive=directive,
            page_context={
                "page_number": 4, "source_hash": source_hash,
                "body_text": "Locked Word fact.", "page_comments": [],
            },
            timeout=2,
            invoke=invoke,
            transport=FakeTransport({
                "https://cdn.example/photo.png": DownloadResponse(
                    200, {"content-type": "image/png"}, payload,
                ),
            }),
        )

    run("a" * 64)
    run("b" * 64)
    assert calls == 2


def test_same_material_id_on_two_pages_uses_one_live_search_and_page_local_references(
    tmp_path: Path,
) -> None:
    directive = _directive()
    payload = _png()
    calls = 0

    def invoke(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return _result([_candidate()])

    first = search_visual_material(
        tmp_path, directive=directive,
        page_context={"page_number": 4, "source_hash": "a" * 64, "body_text": "first", "page_comments": []},
        timeout=10, invoke=invoke,
        transport=FakeTransport({"https://cdn.example/photo.png": DownloadResponse(200, {"content-type": "image/png"}, payload)}),
    )
    second = search_visual_material(
        tmp_path, directive=directive,
        page_context={"page_number": 5, "source_hash": "b" * 64, "body_text": "second", "page_comments": []},
        timeout=10, invoke=invoke, transport=FakeTransport({}),
    )

    assert calls == 1
    assert first[0]["sha256"] == second[0]["sha256"]
    assert first[0]["material_attestation_path"] != second[0]["material_attestation_path"]
    assert "/page_005/" in second[0]["material_attestation_path"]


def test_stale_cached_search_image_fails_closed_without_new_live_call(tmp_path: Path) -> None:
    directive = _directive()
    payload = _png()
    calls = 0

    def invoke(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return _result([_candidate()])

    context = {"page_number": 5, "source_hash": "a" * 64, "body_text": "Locked", "page_comments": []}
    material = search_visual_material(
        tmp_path, directive=directive, page_context=context, timeout=2, invoke=invoke,
        transport=FakeTransport({
            "https://cdn.example/photo.png": DownloadResponse(
                200, {"content-type": "image/png"}, payload,
            ),
        }),
    )[0]
    (tmp_path / material["local_path"]).write_bytes(_png(33, 24))

    with pytest.raises(SearchMaterialBlocked, match="SHA-256|attestation|cache"):
        search_visual_material(
            tmp_path, directive=directive, page_context=context, timeout=2, invoke=invoke,
            transport=FakeTransport({}),
        )
    assert calls == 1


def test_active_search_lease_blocks_duplicate_app_server_call(tmp_path: Path) -> None:
    directive = _directive(required=True)
    page_context = {"page_number": 3, "body_text": "Locked Word fact."}
    budget = codex_web_material_gateway.ResourceBudget()
    directive_id, query, required, material_id = codex_web_material_gateway._request_identity(
        directive
    )
    identity = codex_web_material_gateway._search_cache_identity(
        page_context=page_context,
        directive_id=directive_id,
        material_id=material_id,
        query=query,
        required=required,
        budget=budget,
    )
    deadline = time.monotonic() + 2
    cache_path = codex_web_material_gateway._search_cache_path(
        tmp_path.resolve(), identity, deadline=deadline
    )
    lease_path = codex_web_material_gateway._search_lease_path(cache_path)
    lease_path.write_text("owned-by-another-worker", encoding="utf-8")
    calls = []

    with pytest.raises(SearchMaterialBlocked, match="already in progress") as caught:
        search_visual_material(
            tmp_path,
            directive=directive,
            page_context=page_context,
            timeout=2,
            budget=budget,
            invoke=lambda *_args, **_kwargs: calls.append(True) or _result([]),
            transport=FakeTransport({}),
        )

    assert caught.value.state == "material_resolution_pending"
    assert calls == []


def test_stale_search_lease_is_recovered_atomically(tmp_path: Path) -> None:
    cache_path = tmp_path / "evidence" / "search-cache-test.json"
    cache_path.parent.mkdir()
    lease_path = codex_web_material_gateway._search_lease_path(cache_path)
    lease_path.write_text(
        json.dumps({"token": "abandoned", "expires_at": time.time() - 10}),
        encoding="ascii",
    )

    token = codex_web_material_gateway._acquire_search_lease(cache_path, ttl_seconds=1)
    payload = json.loads(lease_path.read_text(encoding="ascii"))
    assert payload["token"] == token
    assert payload["expires_at"] > time.time()
    codex_web_material_gateway._release_search_lease(cache_path, token)
    assert not lease_path.exists()


def test_batch_lease_ttl_covers_all_bounded_shard_and_commit_operations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ttl = codex_web_material_gateway._batch_lease_ttl_seconds(
        request_count=20, shard_size=5, max_search_concurrency=2,
        max_download_concurrency=3, search_timeout=300,
    )
    assert ttl >= 4980.0
    cache_path = tmp_path / "evidence" / "search-cache-test.json"
    cache_path.parent.mkdir()
    token = codex_web_material_gateway._acquire_search_lease(cache_path, ttl_seconds=ttl)
    lease_path = codex_web_material_gateway._search_lease_path(cache_path)
    expires_at = json.loads(lease_path.read_text(encoding="ascii"))["expires_at"]
    monkeypatch.setattr(codex_web_material_gateway.time, "time", lambda: expires_at - 1.0)

    with pytest.raises(FileExistsError):
        codex_web_material_gateway._acquire_search_lease(cache_path, ttl_seconds=ttl)

    codex_web_material_gateway._release_search_lease(cache_path, token)


def test_single_search_lease_cannot_be_taken_during_three_legal_candidates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ttl = codex_web_material_gateway._single_search_lease_ttl_seconds(300)
    assert ttl >= 2100.0
    cache_path = tmp_path / "evidence" / "single-search-cache.json"
    cache_path.parent.mkdir()
    token = codex_web_material_gateway._acquire_search_lease(cache_path, ttl_seconds=ttl)
    lease_path = codex_web_material_gateway._search_lease_path(cache_path)
    expires_at = json.loads(lease_path.read_text(encoding="ascii"))["expires_at"]
    monkeypatch.setattr(codex_web_material_gateway.time, "time", lambda: expires_at - 1.0)

    with pytest.raises(FileExistsError):
        codex_web_material_gateway._acquire_search_lease(cache_path, ttl_seconds=ttl)

    codex_web_material_gateway._release_search_lease(cache_path, token)


def test_replaced_search_lease_fences_old_worker_cache_commit(tmp_path: Path) -> None:
    cache_path = tmp_path / "evidence" / "search-cache-test.json"
    cache_path.parent.mkdir()
    old_token = codex_web_material_gateway._acquire_search_lease(cache_path, ttl_seconds=1)
    lease_path = codex_web_material_gateway._search_lease_path(cache_path)
    lease_path.write_text(
        json.dumps({"token": old_token, "expires_at": time.time() - 10}),
        encoding="ascii",
    )
    new_token = codex_web_material_gateway._acquire_search_lease(cache_path, ttl_seconds=60)

    with pytest.raises(SearchMaterialBlocked, match="ownership changed"):
        codex_web_material_gateway._write_search_cache(
            tmp_path,
            cache_path,
            identity={"page_number": 1},
            materials=[],
            deadline=time.monotonic() + 2,
            lease_token=old_token,
        )

    codex_web_material_gateway._release_search_lease(cache_path, new_token)


def test_search_cache_copied_from_another_project_fails_closed_without_live_call(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source-project"
    target = tmp_path / "target-project"
    source.mkdir()
    target.mkdir()
    directive = _directive(required=True)
    context = {"page_number": 6, "source_hash": "a" * 64, "body_text": "Locked"}
    calls = 0

    def invoke(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return _result([_candidate()])

    search_visual_material(
        source,
        directive=directive,
        page_context=context,
        timeout=2,
        invoke=invoke,
        transport=FakeTransport({
            "https://cdn.example/photo.png": DownloadResponse(
                200, {"content-type": "image/png"}, _png(),
            ),
        }),
    )
    shutil.copytree(source / "03_evidence", target / "03_evidence")

    with pytest.raises(SearchMaterialBlocked, match="signature|identity"):
        search_visual_material(
            target,
            directive=directive,
            page_context=context,
            timeout=2,
            invoke=invoke,
            transport=FakeTransport({}),
        )

    assert calls == 1


@pytest.mark.parametrize(
    ("mutation", "value"),
    [
        ("safe_trace_missing_model", None),
        ("safe_trace_thread", "different-thread"),
        ("turn_id", "different-turn"),
        ("model", "different-model"),
        ("model_provider", "different-provider"),
        ("usage", {"inputTokens": 999}),
        ("status", "partial"),
        ("request_path", "../outside-request.json"),
        ("request_sha256", "0" * 64),
        ("raw_response_path", "../outside-response.json"),
        ("raw_response_sha256", "0" * 64),
    ],
)
def test_locally_resigned_semantic_trace_tamper_fails_cached_replay_without_live_fallback(
    tmp_path: Path, mutation: str, value,
) -> None:
    directive = _directive(required=True)
    context = {"page_number": 8, "source_hash": "a" * 64, "body_text": "Locked"}
    test_timeout = 10
    material = search_visual_material(
        tmp_path,
        directive=directive,
        page_context=context,
        timeout=test_timeout,
        invoke=lambda *_args, **_kwargs: _result([_candidate()]),
        transport=FakeTransport({
            "https://cdn.example/photo.png": DownloadResponse(
                200, {"content-type": "image/png"}, _png(),
            ),
        }),
    )[0]
    invocation_path = tmp_path / material["invocation_bundle_path"]
    invocation = json.loads(invocation_path.read_text(encoding="utf-8"))
    invocation.pop("signature")
    invocation.pop("sealed_sha256")
    if mutation == "safe_trace_missing_model":
        invocation["safe_trace"].pop("model")
    elif mutation == "safe_trace_thread":
        invocation["safe_trace"]["thread_id"] = value
    elif mutation in {"request_path", "request_sha256"}:
        invocation["request"][mutation.removeprefix("request_")] = value
    elif mutation in {"raw_response_path", "raw_response_sha256"}:
        invocation["raw_response"][mutation.removeprefix("raw_response_")] = value
    else:
        invocation[mutation] = value
    invocation["sealed_sha256"] = hashlib.sha256(
        codex_web_material_gateway._canonical_bytes(invocation)
    ).hexdigest()
    invocation["signature"] = hmac.new(
        codex_web_material_gateway._attestation_key(
            tmp_path.resolve(), deadline=time.monotonic() + test_timeout,
        ),
        codex_web_material_gateway._canonical_bytes(invocation),
        hashlib.sha256,
    ).hexdigest()
    invocation_path.write_text(
        json.dumps(invocation, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    cache_path = next(tmp_path.rglob("search-cache-*.json"))
    cache = json.loads(cache_path.read_text(encoding="utf-8"))
    cached_material = cache["materials"][0]
    cached_material["invocation_bundle_sha256"] = hashlib.sha256(
        invocation_path.read_bytes()
    ).hexdigest()
    cached_material["invocation_bundle_signature"] = invocation["signature"]
    cached_material["invocation_bundle_sealed_sha256"] = invocation["sealed_sha256"]
    cache.pop("signature")
    cache.pop("sealed_sha256")
    cache["sealed_sha256"] = hashlib.sha256(
        codex_web_material_gateway._canonical_bytes(cache)
    ).hexdigest()
    cache["signature"] = hmac.new(
        codex_web_material_gateway._attestation_key(
            tmp_path.resolve(), deadline=time.monotonic() + test_timeout,
        ),
        codex_web_material_gateway._canonical_bytes(cache),
        hashlib.sha256,
    ).hexdigest()
    cache_path.write_text(
        json.dumps(cache, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    live_calls = []

    with pytest.raises(SearchMaterialBlocked, match="trace|invocation|request|response|status"):
        search_visual_material(
            tmp_path,
            directive=directive,
            page_context=context,
            timeout=test_timeout,
            invoke=lambda *_args, **_kwargs: live_calls.append(True) or _result([]),
            transport=FakeTransport({}),
        )

    assert live_calls == []


def test_downloader_normalizes_exif_orientation_before_persisting(tmp_path: Path) -> None:
    stream = io.BytesIO()
    image = Image.new("RGB", (3, 2), (100, 50, 20))
    exif = image.getexif()
    exif[274] = 6
    image.save(stream, format="JPEG", exif=exif)
    transport = FakeTransport(
        {"https://cdn.example/photo.png": DownloadResponse(200, {"content-type": "image/jpeg"}, stream.getvalue())}
    )

    material = download_visual_material(
        tmp_path,
        page_number=1,
        material_id=search_material_id("photo"),
        directive_id="directive-1",
        candidates=[_candidate()],
        timeout=2,
        transport=transport,
    )[0]

    assert (material["width"], material["height"]) == (2, 3)
    with Image.open(tmp_path / material["local_path"]) as persisted:
        assert persisted.getexif().get(274, 1) == 1


def test_missing_required_search_material_blocks_before_generation(tmp_path: Path) -> None:
    with pytest.raises(SearchMaterialBlocked) as caught:
        search_visual_material(
            tmp_path,
            directive=_directive(required=True),
            page_context={"page_number": 2, "body_text": "Locked Word fact."},
            timeout=2,
            invoke=lambda *_args, **_kwargs: _result([]),
            transport=FakeTransport({}),
        )

    assert caught.value.state == "material_blocked"
    assert caught.value.code == "required_search_material_unavailable"


def test_changed_page_authority_keeps_material_id_but_gets_distinct_turn_bundle(tmp_path: Path) -> None:
    directive = _directive()
    payload = _png()

    def invoke_for(turn_id):
        result = _result([_candidate()])
        trace = dict(result.safe_trace)
        trace.update(thread_id=f"thread-{turn_id}", turn_id=turn_id)
        return CodexStructuredResult(
            value=result.value,
            thread_id=f"thread-{turn_id}",
            turn_id=turn_id,
            model=result.model,
            model_provider=result.model_provider,
            auth_mode=result.auth_mode,
            plan_type=result.plan_type,
            usage=result.usage,
            safe_trace=trace,
        )

    first = search_visual_material(
        tmp_path,
        directive=directive,
        page_context={
            "page_number": 3, "source_hash": "a" * 64, "body_text": "Locked Word fact."
        },
        timeout=2,
        invoke=lambda *_args, **_kwargs: invoke_for("turn-one"),
        transport=FakeTransport(
            {"https://cdn.example/photo.png": DownloadResponse(200, {"content-type": "image/png"}, payload)}
        ),
    )[0]
    second = search_visual_material(
        tmp_path,
        directive=directive,
        page_context={
            "page_number": 3, "source_hash": "b" * 64, "body_text": "Locked Word fact."
        },
        timeout=2,
        invoke=lambda *_args, **_kwargs: invoke_for("turn-two"),
        transport=FakeTransport(
            {"https://cdn.example/photo.png": DownloadResponse(200, {"content-type": "image/png"}, payload)}
        ),
    )[0]

    assert first["material_id"] == second["material_id"]
    assert first["invocation_bundle_path"] != second["invocation_bundle_path"]
    assert json.loads((tmp_path / first["invocation_bundle_path"]).read_text())["turn_id"] == "turn-one"
    assert json.loads((tmp_path / second["invocation_bundle_path"]).read_text())["turn_id"] == "turn-two"


def test_advisory_search_can_continue_without_a_candidate(tmp_path: Path) -> None:
    with pytest.warns(RuntimeWarning, match="advisory visual material"):
        materials = search_visual_material(
            tmp_path,
            directive=_directive(required=False),
            page_context={"page_number": 2, "body_text": "Locked Word fact."},
            timeout=2,
            invoke=lambda *_args, **_kwargs: _result([]),
            transport=FakeTransport({}),
        )
    assert materials == []


def test_material_bundle_retrieves_resolved_request_with_same_stable_id(
    tmp_path: Path, monkeypatch
) -> None:
    project, source_sha, _style_sha = _project(tmp_path)
    comments = [
        {
            "comment_id": "news-photo",
            "text": "[search-evidence:Operation Phoenix photo]",
            "author": "reviewer",
            "timestamp": None,
        }
    ]
    captured = {}

    real_search = search_visual_material
    payload = _png()

    def fake_search(project_arg, *, directives, page_context, timeout, budget, **_kwargs):
        assert len(directives) == 1
        directive = directives[0]
        captured.update(
            project=project_arg,
            directive=directive,
            page_context=page_context,
            timeout=timeout,
            budget=budget,
        )
        return [real_search(
            project_arg,
            directive=directive,
            page_context=page_context,
            timeout=timeout,
            budget=budget,
            invoke=lambda *_args, **_kwargs: _result([_candidate()]),
            transport=FakeTransport(
                {
                    "https://cdn.example/photo.png": DownloadResponse(
                        200, {"content-type": "image/png"}, payload
                    )
                }
            ),
        )]

    monkeypatch.setattr(page_material_bundle_v4, "search_visual_materials", fake_search)
    bundle = _build(project, source_sha, _contract(project, comments=comments))

    material_id = bundle["comment_intents"][0]["material_id"]
    assert captured["project"] == project
    assert captured["directive"].directive_id
    assert captured["directive"].search_query == "Operation Phoenix photo"
    assert captured["page_context"]["body_text"] == bundle["authoritative_content"]["body_text"]
    assert len(bundle["search_evidence"]) == 1
    evidence = bundle["search_evidence"][0]
    assert evidence["asset_id"] == material_id
    assert evidence["query"] == "Operation Phoenix photo"
    assert evidence["source_url"] == "https://news.example/story"
    assert evidence["excerpt"] == "Executives at the public press briefing."
    assert evidence["sha256"] == hashlib.sha256(payload).hexdigest()
    assert evidence["local_path"].endswith(".png")
    assert evidence["material_attestation"]["signature"]


def test_material_bundle_propagates_required_material_block_before_image2(
    tmp_path: Path, monkeypatch
) -> None:
    project, source_sha, _style_sha = _project(tmp_path)
    comments = [
        {
            "comment_id": "news-photo",
            "text": "[search-evidence:Operation Phoenix photo]",
            "author": "reviewer",
            "timestamp": None,
        }
    ]

    def blocked(*_args, **_kwargs):
        raise SearchMaterialBlocked("no qualifying real image with source")

    monkeypatch.setattr(page_material_bundle_v4, "search_visual_materials", blocked)
    with pytest.raises(SearchMaterialBlocked) as caught:
        _build(project, source_sha, _contract(project, comments=comments))

    assert caught.value.state == "material_blocked"
    assert caught.value.code == "required_search_material_unavailable"


def test_material_bundle_limits_selected_search_images_across_the_page(
    tmp_path: Path, monkeypatch
) -> None:
    project, source_sha, _style_sha = _project(tmp_path)
    comments = [
        {"comment_id": "one", "text": "[search-evidence:first photo]", "author": "", "timestamp": None},
        {"comment_id": "two", "text": "[search-evidence:second photo]", "author": "", "timestamp": None},
    ]

    real_search = search_visual_material
    payload = _png()
    requested_urls = []

    def two_results(project_arg, *, directives, page_context, timeout, budget, **_kwargs):
        outcomes = []
        for directive_index, directive in enumerate(directives):
            urls = [
                f"https://cdn.example/{directive.source_comment_id}-{index}.png"
                for index in (1, 2)
            ]
            transport = FakeTransport(
                {
                    url: DownloadResponse(
                        200, {"content-type": "image/png"},
                        _png(32 + index + (directive_index * 4), 24),
                    )
                    for index, url in enumerate(urls)
                }
            )
            outcomes.append(real_search(
                project_arg,
                directive=directive,
                page_context=page_context,
                timeout=timeout,
                budget=budget,
                invoke=lambda *_args, _urls=urls, **_kwargs: _result(
                    [_candidate(direct_image_url=url) for url in _urls]
                ),
                transport=transport,
            ))
            requested_urls.extend(transport.requests)
        return outcomes

    monkeypatch.setattr(page_material_bundle_v4, "search_visual_materials", two_results)
    bundle = _build(project, source_sha, _contract(project, comments=comments))
    assert len(bundle["search_evidence"]) == 3
    assert len(requested_urls) == 3


def test_installed_app_server_v2_schema_declares_all_web_search_modes() -> None:
    executable = shutil.which("codex")
    if not executable:
        pytest.skip("installed Codex runtime is unavailable in this test environment")

    modes = codex_subscription_runtime.app_server_web_search_modes(
        [executable, "app-server", "--stdio"], timeout=15
    )

    assert modes == frozenset({"disabled", "cached", "indexed", "live"})


def test_live_search_fails_closed_when_installed_protocol_capability_is_missing(
    tmp_path: Path,
) -> None:
    command, capture = _protocol_server(tmp_path)

    def missing(_command, _timeout):
        raise CodexRuntimeUnavailable("installed App Server schema has no live web_search")

    with pytest.raises(CodexRuntimeUnavailable, match="no live web_search"):
        invoke_structured(
            tmp_path,
            role="visual-material-search",
            prompt="Search.",
            images=[],
            output_schema={"type": "object"},
            timeout=5,
            command=command,
            web_search="live",
            capability_probe=missing,
        )
    assert not capture.exists()


@pytest.mark.parametrize(
    "modes",
    [
        frozenset({"disabled", "cached", "indexed"}),
        frozenset({"disabled", "cached", "indexed", "live", "unexpected"}),
    ],
    ids=["missing-mode", "unknown-extra-mode"],
)
def test_live_search_requires_exact_installed_web_search_modes(
    tmp_path: Path, modes: frozenset[str]
) -> None:
    command, capture = _protocol_server(tmp_path)

    with pytest.raises(CodexRuntimeUnavailable, match="required web_search modes"):
        invoke_structured(
            tmp_path,
            role="visual-material-search",
            prompt="Search.",
            images=[],
            output_schema={"type": "object"},
            timeout=5,
            command=command,
            web_search="live",
            capability_probe=lambda _command, _timeout: modes,
        )
    assert not capture.exists()


def test_subscription_client_identity_is_260(tmp_path: Path) -> None:
    schema = {
        "type": "object",
        "additionalProperties": False,
        "required": ["ok"],
        "properties": {"ok": {"const": True}},
    }
    command, capture = _protocol_server(tmp_path)
    invoke_structured(
        tmp_path,
        role="visual-material-search",
        prompt="Search.",
        images=[],
        output_schema=schema,
        timeout=5,
        command=command,
        web_search="live",
        capability_probe=lambda _command, _timeout: frozenset(
            {"disabled", "cached", "indexed", "live"}
        ),
    )
    requests = json.loads(capture.read_text(encoding="utf-8"))
    initialize = next(item for item in requests if item.get("method") == "initialize")
    assert initialize["params"]["clientInfo"]["version"] == "1.2.0"


def test_downloader_rejects_explicit_port_zero(tmp_path: Path) -> None:
    with pytest.raises(SearchMaterialBlocked, match="port"):
        download_visual_material(
            tmp_path,
            page_number=1,
            material_id=search_material_id("photo"),
            directive_id="directive-1",
            candidates=[_candidate(direct_image_url="https://cdn.example:0/photo.png")],
            timeout=2,
            transport=FakeTransport({}),
        )


def test_downloader_rejects_excessive_single_edge_before_full_decode(tmp_path: Path) -> None:
    payload = _png(12_001, 1)
    transport = FakeTransport(
        {"https://cdn.example/photo.png": DownloadResponse(200, {"content-type": "image/png"}, payload)}
    )
    with pytest.raises(SearchMaterialBlocked, match="dimension"):
        download_visual_material(
            tmp_path,
            page_number=1,
            material_id=search_material_id("photo"),
            directive_id="directive-1",
            candidates=[_candidate()],
            timeout=2,
            transport=transport,
        )


def test_dns_resolution_cannot_outlive_the_absolute_deadline(tmp_path: Path) -> None:
    class SlowResolver(FakeTransport):
        def resolve(self, hostname: str, port: int):
            time.sleep(0.5)
            return ["93.184.216.34"]

    started = time.monotonic()
    with pytest.raises(SearchMaterialBlocked, match="DNS|timed out"):
        download_visual_material(
            tmp_path,
            page_number=1,
            material_id=search_material_id("photo"),
            directive_id="directive-1",
            candidates=[_candidate()],
            timeout=0.05,
            transport=SlowResolver({}),
        )
    assert time.monotonic() - started < 0.3


def test_isolated_operations_are_terminated_at_the_shared_deadline() -> None:
    deadline = time.monotonic() + 0.05
    with pytest.raises(SearchMaterialBlocked, match="timed out"):
        codex_web_material_gateway._run_isolated(
            _slow_isolated_operation, (0.5,), deadline=deadline, label="image decode"
        )
    assert time.monotonic() < deadline + 0.3


def test_shared_resource_budget_blocks_next_network_before_page_overflow(tmp_path: Path) -> None:
    payload = _png()
    budget = codex_web_material_gateway.ResourceBudget(max_images=1)
    first_url = "https://cdn.example/one.png"
    second_url = "https://cdn.example/two.png"
    transport = FakeTransport(
        {
            first_url: DownloadResponse(200, {"content-type": "image/png"}, payload),
            second_url: DownloadResponse(200, {"content-type": "image/png"}, payload),
        }
    )
    deadline = time.monotonic() + 5
    first = download_visual_material(
        tmp_path,
        page_number=1,
        material_id=search_material_id("one"),
        directive_id="directive-one",
        query="one",
        candidates=[_candidate(direct_image_url=first_url)],
        timeout=5,
        deadline=deadline,
        budget=budget,
        transport=transport,
    )
    assert len(first) == 1

    with pytest.raises(SearchMaterialBlocked, match="image count"):
        download_visual_material(
            tmp_path,
            page_number=1,
            material_id=search_material_id("two"),
            directive_id="directive-two",
            query="two",
            candidates=[_candidate(direct_image_url=second_url)],
            timeout=5,
            deadline=deadline,
            budget=budget,
            transport=transport,
        )
    assert [request[0] for request in transport.requests] == [first_url]


def test_shared_resource_budget_counts_network_bytes_across_directives(tmp_path: Path) -> None:
    payload = _png()
    budget = codex_web_material_gateway.ResourceBudget(
        max_images=3,
        max_network_bytes=len(payload),
    )
    first_url = "https://cdn.example/one.png"
    second_url = "https://cdn.example/two.png"
    transport = FakeTransport(
        {
            first_url: DownloadResponse(200, {"content-type": "image/png"}, payload),
            second_url: DownloadResponse(200, {"content-type": "image/png"}, payload),
        }
    )
    deadline = time.monotonic() + 5
    download_visual_material(
        tmp_path,
        page_number=1,
        material_id=search_material_id("one"),
        directive_id="directive-one",
        query="one",
        candidates=[_candidate(direct_image_url=first_url)],
        timeout=5,
        deadline=deadline,
        budget=budget,
        transport=transport,
    )
    with pytest.raises(SearchMaterialBlocked, match="network byte"):
        download_visual_material(
            tmp_path,
            page_number=1,
            material_id=search_material_id("two"),
            directive_id="directive-two",
            query="two",
            candidates=[_candidate(direct_image_url=second_url)],
            timeout=5,
            deadline=deadline,
            budget=budget,
            transport=transport,
        )
    assert [request[0] for request in transport.requests] == [first_url]


def test_material_attestation_detects_file_and_record_tampering(tmp_path: Path) -> None:
    directive = _directive()
    payload = _png()
    material = search_visual_material(
        tmp_path,
        directive=directive,
        page_context={"page_number": 1, "body_text": "Locked Word fact."},
        timeout=5,
        invoke=lambda *_args, **_kwargs: _result([_candidate()]),
        transport=FakeTransport(
            {"https://cdn.example/photo.png": DownloadResponse(200, {"content-type": "image/png"}, payload)}
        ),
    )[0]

    verified = codex_web_material_gateway.verify_search_material(
        tmp_path,
        material,
        expected_material_id=directive["decisions"][0]["material_id"],
        expected_directive_id=directive["directive_id"],
        expected_query=directive["search_query"],
        deadline=time.monotonic() + 5,
    )
    assert verified["sha256"] == hashlib.sha256(payload).hexdigest()
    assert verified["record_timestamp"].endswith("Z")
    assert verified["material_attestation_signature"]

    (tmp_path / verified["local_path"]).write_bytes(_png(33, 24))
    with pytest.raises(SearchMaterialBlocked, match="file|SHA"):
        codex_web_material_gateway.verify_search_material(
            tmp_path,
            material,
            expected_material_id=directive["decisions"][0]["material_id"],
            expected_directive_id=directive["directive_id"],
            expected_query=directive["search_query"],
            deadline=time.monotonic() + 5,
        )


def test_material_attestation_cannot_be_resealed_without_the_key(tmp_path: Path) -> None:
    directive = _directive()
    payload = _png()
    material = search_visual_material(
        tmp_path,
        directive=directive,
        page_context={"page_number": 1, "body_text": "Locked Word fact."},
        timeout=5,
        invoke=lambda *_args, **_kwargs: _result([_candidate()]),
        transport=FakeTransport(
            {"https://cdn.example/photo.png": DownloadResponse(200, {"content-type": "image/png"}, payload)}
        ),
    )[0]
    record_path = tmp_path / material["material_attestation_path"]
    record = json.loads(record_path.read_text(encoding="utf-8"))
    record["attestation"]["publisher"] = "Forged Publisher"
    record["sealed_sha256"] = hashlib.sha256(
        json.dumps(record["attestation"], sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    record_path.write_text(json.dumps(record), encoding="utf-8")

    with pytest.raises(SearchMaterialBlocked, match="signature|digest"):
        codex_web_material_gateway.verify_search_material(
            tmp_path,
            material,
            expected_material_id=directive["decisions"][0]["material_id"],
            expected_directive_id=directive["directive_id"],
            expected_query=directive["search_query"],
            deadline=time.monotonic() + 5,
        )


def test_atomic_publish_and_key_creation_are_race_safe(tmp_path: Path) -> None:
    target = tmp_path / "evidence.bin"
    deadline = time.monotonic() + 5
    errors = []

    def publish(payload):
        try:
            codex_web_material_gateway._write_immutable(
                target, payload, deadline=deadline
            )
        except BaseException as exc:  # capture worker outcome for the assertion
            errors.append(exc)

    threads = [
        threading.Thread(target=publish, args=(b"A" * 4096,)),
        threading.Thread(target=publish, args=(b"B" * 4096,)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert target.read_bytes() in {b"A" * 4096, b"B" * 4096}
    assert len(errors) == 1 and isinstance(errors[0], SearchMaterialBlocked)
    assert not list(tmp_path.glob("*.tmp"))

    keys = []
    key_errors = []

    def create_key():
        try:
            keys.append(codex_web_material_gateway._attestation_key(tmp_path, deadline=deadline))
        except BaseException as exc:
            key_errors.append(exc)

    key_threads = [
        threading.Thread(target=create_key)
        for _ in range(8)
    ]
    for thread in key_threads:
        thread.start()
    for thread in key_threads:
        thread.join()
    assert not key_errors
    assert len(keys) == 8 and len(set(keys)) == 1


@pytest.mark.parametrize(
    "program",
    [_BLOCKED_WRITE_PROGRAM, _BLOCKED_FSYNC_PROGRAM],
    ids=["blocked-write", "blocked-fsync"],
)
def test_atomic_publish_hard_bounds_blocked_temp_write_and_cleans_up(
    tmp_path: Path, monkeypatch, program: str
) -> None:
    target = tmp_path / "deadline.bin"
    started = time.monotonic()
    monkeypatch.setattr(codex_web_material_gateway, "_WRITE_PROCESS_PROGRAM", program)

    with pytest.raises(SearchMaterialBlocked, match="evidence write timed out"):
        codex_web_material_gateway._write_immutable(
            target, b"deadline-bound", deadline=started + 0.05
        )

    assert time.monotonic() - started < 0.25
    assert not target.exists()
    assert not list(tmp_path.glob(".*.tmp"))


def test_batch_search_reuses_project_discovery_on_another_page(tmp_path: Path) -> None:
    directive = _batch_directive(1)
    candidate = _candidate(
        direct_image_url="https://cdn.example/shared-logo.png",
        matched_entities=[directive["entity"]], title="Shared logo",
    )
    calls = 0

    def invoke(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return _batch_result([_batch_item(directive, candidate=candidate)])

    first = search_visual_materials(
        tmp_path, directives=[directive], page_context={"page_number": 12, "body_text": "one"},
        timeout=10, invoke=invoke,
        transport=FakeTransport({candidate["direct_image_url"]: DownloadResponse(
            200, {"content-type": "image/png"}, _png(),
        )}),
    )
    second = search_visual_materials(
        tmp_path, directives=[directive], page_context={"page_number": 13, "body_text": "two"},
        timeout=10, invoke=invoke, transport=FakeTransport({}),
    )

    assert calls == 1
    assert first[0][0]["sha256"] == second[0][0]["sha256"]
    assert "/page_013/" in second[0][0]["material_attestation_path"]


def test_atomic_publish_rejects_reserved_temp_replacement_without_truncating_it(
    tmp_path: Path, monkeypatch,
) -> None:
    """The child must bind to the inode reserved by the parent before truncating."""
    target = tmp_path / "published.bin"
    replacement = tmp_path / "replacement.bin"
    replacement.write_bytes(b"must-survive")
    original = codex_web_material_gateway._run_write_process

    def replace_before_child_open(path: Path, payload: bytes, **kwargs) -> None:
        path.unlink()
        os.link(replacement, path)
        original(path, payload, **kwargs)

    monkeypatch.setattr(
        codex_web_material_gateway, "_run_write_process", replace_before_child_open,
    )

    with pytest.raises(SearchMaterialBlocked, match="identity changed"):
        codex_web_material_gateway._write_immutable(
            target, b"attacker-controlled", deadline=time.monotonic() + 2.0,
        )

    assert replacement.read_bytes() == b"must-survive"
    assert not target.exists()
    time.sleep(0.4)
    assert not target.exists()
    assert not list(tmp_path.glob(".*.tmp"))


def test_v6_reference_gateway_emits_one_bounded_orchestrator_item_without_downloading(tmp_path: Path, monkeypatch):
    """Autonomous downloading would make a V6 source URL a second Python web client."""
    receipt_path = tmp_path / "02_v6/reference_materials/page_001.json"
    receipt_path.parent.mkdir(parents=True)
    receipt_path.write_text(json.dumps({
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
    monkeypatch.setattr(codex_web_material_gateway, "download_visual_material", lambda *_a, **_k: pytest.fail("downloaded"))

    items = codex_web_material_gateway.emit_reference_work_items(tmp_path)

    assert items == [{
        "page_number": 1,
        "request_id": "request-1",
        "purpose": "verified storefront image",
        "identity_evidence_need": "show the named storefront",
        "status": "pending",
        "max_results": 1,
        "commands": {
            "import_reference": "workflow_v6_cli.py import-reference --project <project> --page 1 --request-id request-1 --image <local-file> [--source-url <metadata-url>]",
            "fail_reference": "workflow_v6_cli.py fail-reference --project <project> --page 1 --request-id request-1 --reason <reason>",
            "reject_reference": "workflow_v6_cli.py reject-reference --project <project> --page 1 --request-id request-1 --reason <reason>",
        },
    }]
