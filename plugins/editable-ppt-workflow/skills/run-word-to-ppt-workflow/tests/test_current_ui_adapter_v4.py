"""Current Confirm UI adapter contract for the additive V4 foundation."""

from __future__ import annotations

import copy
import hashlib
import json
import shutil
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
TESTS = ROOT / "tests"
for path in (SCRIPTS, TESTS):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from current_ui_adapter import CURRENT_UI_PAYLOAD_VERSION, adapt_current_ui_payload  # noqa: E402
from codex_web_material_gateway import sign_project_payload  # noqa: E402
from codex_subscription_runtime import CodexStructuredResult  # noqa: E402
from style_contract import canonical_confirmation, canonical_json_bytes, compile_style_execution  # noqa: E402
from page_requirement_summary import (  # noqa: E402
    build_page_requirement_summary,
    load_verified_page_resolutions,
    public_requirement_summary,
    verify_page_requirement_summary,
)
from test_style_contract import confirmed_result  # noqa: E402


def _multipage_summary_project(
    project: Path, *, first_page_comments: list[dict] | None = None,
    second_page_comments: list[dict] | None = None, invoke=None,
) -> tuple[list[dict], dict]:
    contracts = [{
        "page_number": page,
        "page_title": f"第{page}页",
        "body_text": f"正文{page}",
        "source_text": f"正文{page}",
        "source_tables": [],
        "page_comments": (
            copy.deepcopy(first_page_comments or []) if page == 1
            else copy.deepcopy(second_page_comments or []) if page == 2
            else []
        ),
        "asset_bindings": [],
    } for page in (1, 2, 3)]
    source = project / "00_source/source.docx"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"word")
    contract_dir = project / "01_page_contracts"
    contract_dir.mkdir()
    jobs = []
    lock_pages = []
    for contract in contracts:
        page = contract["page_number"]
        path = contract_dir / f"page_{page:03d}.json"
        path.write_text(json.dumps(contract, ensure_ascii=False), encoding="utf-8")
        jobs.append({"page_number": page, "contract_file": f"01_page_contracts/{path.name}"})
        lock_pages.append({
            "page_number": page,
            "contract_file": path.name,
            "contract_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        })
    (contract_dir / "source_lock.json").write_text(
        json.dumps({"pages": lock_pages}), encoding="utf-8",
    )
    (project / "workflow_run.json").write_text(json.dumps({
        "word_source": {
            "path": "00_source/source.docx",
            "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        },
        "pagination": {"page_count": 3, "locked_page_order": [1, 2, 3]},
        "jobs": jobs,
    }), encoding="utf-8")
    return contracts, build_page_requirement_summary(project, contracts, invoke=invoke)


def test_summary_rebuild_reuses_unchanged_fallback_page_without_resolving_it_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import page_requirement_summary as requirement_summary

    resolver_pages = []
    model_calls = []
    real_resolver = requirement_summary.resolve_page_comments

    def counting_resolver(project, contract, assets, timeout, invoke=None):
        resolver_pages.append(contract["page_number"])
        return real_resolver(project, contract, assets, timeout, invoke=invoke)

    def fallback(project, **kwargs):
        model_calls.append(kwargs["prompt"])
        trace = {
            "runtime": "codex-app-server", "role": "comment-resolution",
            "thread_id": "thread-stable", "turn_id": f"turn-{len(model_calls)}",
            "model": "gpt-test", "model_provider": "openai", "auth_mode": "chatgpt",
            "plan_type": "plus", "usage": {},
        }
        return CodexStructuredResult(
            value={
                "kind": "layout_override", "authority_kind": "visual_override",
                "required": True, "search_required": False, "search_query": None,
                "decisions": [{"target": "visual.layout", "action": "set", "value": "spacious"}],
            },
            thread_id=trace["thread_id"], turn_id=trace["turn_id"], model=trace["model"],
            model_provider=trace["model_provider"], auth_mode="chatgpt", plan_type="plus",
            usage={}, safe_trace=trace,
        )

    monkeypatch.setattr(requirement_summary, "resolve_page_comments", counting_resolver)
    contracts, first = _multipage_summary_project(
        tmp_path,
        second_page_comments=[{
            "comment_id": "fallback-2", "text": "让这一页更有呼吸感",
            "author": "", "timestamp": None,
        }],
        invoke=fallback,
    )
    page2_before = copy.deepcopy(first["pages"][1])
    assert len(model_calls) == 1
    resolver_pages.clear()

    contracts[0]["page_comments"] = [{
        "comment_id": "changed-1", "text": "[note:只改变第一页]",
        "author": "", "timestamp": None,
    }]
    page1_path = tmp_path / "01_page_contracts/page_001.json"
    page1_path.write_text(json.dumps(contracts[0], ensure_ascii=False), encoding="utf-8")
    lock_path = tmp_path / "01_page_contracts/source_lock.json"
    lock = json.loads(lock_path.read_text(encoding="utf-8"))
    lock["pages"][0]["contract_sha256"] = hashlib.sha256(page1_path.read_bytes()).hexdigest()
    lock_path.write_text(json.dumps(lock), encoding="utf-8")
    word_path = tmp_path / "00_source/source.docx"
    word_path.write_bytes(b"word-with-page-1-comment-change")
    state_path = tmp_path / "workflow_run.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["word_source"]["sha256"] = hashlib.sha256(word_path.read_bytes()).hexdigest()
    state_path.write_text(json.dumps(state), encoding="utf-8")

    rebuilt = build_page_requirement_summary(tmp_path, contracts, invoke=fallback)

    assert resolver_pages == [1]
    assert len(model_calls) == 1
    assert rebuilt["pages"][1] == page2_before


def _resign_summary(project: Path, artifact: dict) -> None:
    artifact["sealed_sha256"] = hashlib.sha256(json.dumps(
        {key: value for key, value in artifact.items() if key not in {"sealed_sha256", "projectSignature"}},
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")).hexdigest()
    artifact["projectSignature"] = sign_project_payload(
        project,
        {key: value for key, value in artifact.items() if key != "projectSignature"},
        purpose="page-comment-resolution-v1",
    )


def test_current_ui_adapter_preserves_confirmation_and_execution_bytes() -> None:
    """Any adapter-added metadata or reordered semantics would invalidate existing immutable artifacts."""
    raw = confirmed_result()
    confirmation = canonical_confirmation(raw)
    execution = compile_style_execution(raw)
    assert hashlib.sha256(canonical_json_bytes(confirmation)).hexdigest() == (
        "a261036994e2c821c19eb7488d166dee4812e912b7de8c443c21d8448679df96"
    )
    assert hashlib.sha256(canonical_json_bytes(execution)).hexdigest() == (
        "30dae90948c7d8ec75a1dc31a568fd0e9117c5622e2c5ebae2d9b4d72372a92e"
    )


def test_adapter_is_pure_and_projects_only_the_existing_confirmation_semantics() -> None:
    """Mutating the browser result or leaking UI-only fields would make compilation nondeterministic."""
    raw = confirmed_result()
    raw["ui_only_preview_state"] = {"selected_panel": "typography"}
    before = copy.deepcopy(raw)
    adapted = adapt_current_ui_payload(raw, payload_version=CURRENT_UI_PAYLOAD_VERSION)
    assert raw == before
    assert "ui_only_preview_state" not in adapted
    assert adapted == canonical_confirmation(raw)


@pytest.mark.parametrize(
    "version",
    ["confirm-ui-result-v0", "confirm-ui-result-v2", "word-ppt-workflow-v4"],
)
def test_adapter_fails_closed_on_unknown_explicit_payload_versions(version: str) -> None:
    """An unknown version must never fall through to the current field projection."""
    with pytest.raises(ValueError, match="unsupported Confirm UI payload version"):
        adapt_current_ui_payload(confirmed_result(), payload_version=version)


def test_version_declared_in_payload_cannot_disagree_with_adapter_dispatch() -> None:
    """A mismatched embedded version and dispatch version would make the adapter ambiguous."""
    raw = confirmed_result()
    raw["ui_payload_version"] = "confirm-ui-result-v2"
    with pytest.raises(ValueError, match="unsupported Confirm UI payload version"):
        adapt_current_ui_payload(raw, payload_version=CURRENT_UI_PAYLOAD_VERSION)


def test_complete_multipage_summary_is_resolved_once_and_reused_by_every_consumer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import page_requirement_summary as requirement_summary

    calls = 0
    real_resolver = requirement_summary.resolve_page_comments

    def counting_resolver(*args, **kwargs):
        nonlocal calls
        if args[1].get("page_comments"):
            calls += 1
        return real_resolver(*args, **kwargs)

    monkeypatch.setattr(requirement_summary, "resolve_page_comments", counting_resolver)
    contracts, artifact = _multipage_summary_project(tmp_path, first_page_comments=[{
        "comment_id": "c1", "text": "[note:仅供参考]", "author": "", "timestamp": None,
    }])

    assert artifact["page_count"] == 3
    assert verify_page_requirement_summary(tmp_path, artifact)
    assert [item["page"] for item in public_requirement_summary(
        tmp_path, artifact,
    )["pageRequirementSummary"]] == [1, 2, 3]
    for contract in contracts:
        directives, identity = load_verified_page_resolutions(tmp_path, contract)
        assert len(directives) == (1 if contract["page_number"] == 1 else 0)
        assert identity["page_entry_sha256"] == artifact["pages"][contract["page_number"] - 1][
            "pageEntrySha256"
        ]
    assert calls == 1


@pytest.mark.parametrize(
    "mutation",
    [
        "empty", "truncated", "overlong", "duplicate", "reordered", "wrong_page_count",
    ],
)
def test_hmac_valid_incomplete_or_misaligned_page_sets_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutation: str,
) -> None:
    contracts, artifact = _multipage_summary_project(tmp_path)
    forged = copy.deepcopy(artifact)
    if mutation == "empty":
        forged["pages"] = []
    elif mutation == "truncated":
        forged["pages"] = forged["pages"][:2]
    elif mutation == "overlong":
        extra = copy.deepcopy(forged["pages"][-1])
        extra["page"] = 4
        forged["pages"].append(extra)
    elif mutation == "duplicate":
        forged["pages"][1] = copy.deepcopy(forged["pages"][0])
    elif mutation == "reordered":
        forged["pages"][0], forged["pages"][1] = forged["pages"][1], forged["pages"][0]
    elif mutation == "wrong_page_count":
        forged["page_count"] = 2
    _resign_summary(tmp_path, forged)
    summary_path = tmp_path / "confirm_ui/page_requirement_summary.json"
    summary_path.write_text(json.dumps(forged, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(
        "page_requirement_summary.resolve_page_comments",
        lambda *_args, **_kwargs: pytest.fail("invalid artifact must not invoke a resolver/model"),
    )

    assert not verify_page_requirement_summary(tmp_path, forged)
    with pytest.raises(ValueError, match="seal|closure"):
        public_requirement_summary(tmp_path, forged)
    with pytest.raises(ValueError, match="comment_resolution_blocked"):
        load_verified_page_resolutions(tmp_path, contracts[0])


@pytest.mark.parametrize(
    "bad_page", [True, 1.0, "1", None, 0, -1],
    ids=["bool", "float", "numeric-string", "null", "zero", "negative"],
)
@pytest.mark.parametrize(
    "boundary", [
        "artifact", "artifact_locked_order", "workflow_locked_order",
        "job", "contract", "source_lock",
    ],
)
def test_non_exact_integer_page_identities_fail_closed_at_every_authority_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, boundary: str, bad_page: object,
) -> None:
    contracts, artifact = _multipage_summary_project(tmp_path)
    state_path = tmp_path / "workflow_run.json"
    lock_path = tmp_path / "01_page_contracts/source_lock.json"
    contract_path = tmp_path / "01_page_contracts/page_001.json"
    if boundary == "artifact":
        artifact["pages"][0]["page"] = bad_page
    elif boundary == "artifact_locked_order":
        artifact["projectAuthority"]["lockedPageOrder"][0] = bad_page
    elif boundary == "workflow_locked_order":
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state["pagination"]["locked_page_order"][0] = bad_page
        state_path.write_text(json.dumps(state), encoding="utf-8")
    elif boundary == "job":
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state["jobs"][0]["page_number"] = bad_page
        state_path.write_text(json.dumps(state), encoding="utf-8")
    elif boundary == "contract":
        contract = json.loads(contract_path.read_text(encoding="utf-8"))
        contract["page_number"] = bad_page
        contract_path.write_text(json.dumps(contract, ensure_ascii=False), encoding="utf-8")
        lock = json.loads(lock_path.read_text(encoding="utf-8"))
        lock["pages"][0]["contract_sha256"] = hashlib.sha256(contract_path.read_bytes()).hexdigest()
        lock_path.write_text(json.dumps(lock), encoding="utf-8")
        artifact["pages"][0]["pageContractSha256"] = hashlib.sha256(contract_path.read_bytes()).hexdigest()
        artifact["projectAuthority"]["sourceLockSha256"] = hashlib.sha256(lock_path.read_bytes()).hexdigest()
    elif boundary == "source_lock":
        lock = json.loads(lock_path.read_text(encoding="utf-8"))
        lock["pages"][0]["page_number"] = bad_page
        lock_path.write_text(json.dumps(lock), encoding="utf-8")
        artifact["projectAuthority"]["sourceLockSha256"] = hashlib.sha256(lock_path.read_bytes()).hexdigest()
    _resign_summary(tmp_path, artifact)
    summary_path = tmp_path / "confirm_ui/page_requirement_summary.json"
    summary_path.write_text(json.dumps(artifact, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(
        "page_requirement_summary.resolve_page_comments",
        lambda *_args, **_kwargs: pytest.fail("invalid identity must not invoke resolver/model"),
    )

    assert not verify_page_requirement_summary(tmp_path, artifact)
    with pytest.raises(ValueError, match="seal|closure"):
        public_requirement_summary(tmp_path, artifact)
    with pytest.raises(ValueError, match="comment_resolution_blocked"):
        load_verified_page_resolutions(tmp_path, contracts[0])


def test_read_only_summary_is_compiled_from_resolved_directives_not_raw_ui_fields(tmp_path: Path) -> None:
    contracts = [
        {
            "page_number": 1,
            "page_title": "表达要求",
            "body_text": "完整Word原文",
            "source_text": "完整Word原文",
            "source_tables": [],
            "page_comments": [{"comment_id": "c1", "text": "文字表达图片化"}],
            "asset_bindings": [],
        },
        {
            "page_number": 2,
            "page_title": "凤凰行动",
            "body_text": "浙江 凤凰行动 并购生态圈 新闻",
            "source_text": "浙江 凤凰行动 并购生态圈 新闻",
            "source_tables": [],
            "page_comments": [{"comment_id": "c2", "text": "新闻稿图片"}],
            "asset_bindings": [],
        },
        {
            "page_number": 3,
            "page_title": "固定层",
            "body_text": "Logo位于固定区域",
            "source_text": "Logo位于固定区域",
            "source_tables": [],
            "page_comments": [{"comment_id": "c3", "text": "把Logo放入正文"}],
            "asset_bindings": [],
        },
    ]

    source = tmp_path / "00_source" / "source.docx"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"word")
    contract_dir = tmp_path / "01_page_contracts"
    contract_dir.mkdir()
    jobs = []
    lock_pages = []
    for contract in contracts:
        page = contract["page_number"]
        path = contract_dir / f"page_{page:03d}.json"
        path.write_text(json.dumps(contract, ensure_ascii=False), encoding="utf-8")
        jobs.append({"page_number": page, "contract_file": f"01_page_contracts/{path.name}"})
        lock_pages.append({
            "page_number": page,
            "contract_file": path.name,
            "contract_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        })
    (contract_dir / "source_lock.json").write_text(json.dumps({"pages": lock_pages}), encoding="utf-8")
    (tmp_path / "workflow_run.json").write_text(json.dumps({
        "word_source": {"path": "00_source/source.docx", "sha256": hashlib.sha256(source.read_bytes()).hexdigest()},
        "pagination": {"page_count": 3, "locked_page_order": [1, 2, 3]},
        "jobs": jobs,
    }), encoding="utf-8")

    artifact = build_page_requirement_summary(tmp_path, contracts)
    public = public_requirement_summary(tmp_path, artifact)

    assert verify_page_requirement_summary(tmp_path, artifact)
    assert public["pageRequirementSummary"][0]["directives"] == ["文字表达图片化"]
    assert public["pageRequirementSummary"][1]["plannedSearches"]
    assert public["pageRequirementSummary"][1]["materialActions"] == ["搜索并提供外部图片素材"]
    assert public["pageRequirementSummary"][2]["rejectedHardRuleOverrides"] == [
        "fixed_layer_override_rejected"
    ]
    assert all(item["readOnly"] is True for item in public["pageRequirementSummary"])
    assert "closedDirectives" not in public["pageRequirementSummary"][0]

    forged = copy.deepcopy(artifact)
    record = forged["pages"][0]["closedDirectives"][0]
    record["directive"]["text"] = "伪造分页要求"
    record["resolution_receipt"]["raw_comment_sha256"] = hashlib.sha256(
        "伪造分页要求".encode("utf-8")
    ).hexdigest()
    record["resolution_receipt"]["closed_directive_sha256"] = hashlib.sha256(
        json.dumps(record["directive"], ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    forged["pages"][0]["directives"] = ["伪造分页要求"]
    forged["sealed_sha256"] = hashlib.sha256(json.dumps(
        {key: value for key, value in forged.items() if key not in {"sealed_sha256", "projectSignature"}},
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")).hexdigest()
    assert not verify_page_requirement_summary(tmp_path, forged)

    other = tmp_path.parent / f"{tmp_path.name}-other-project"
    shutil.copytree(tmp_path, other)
    (other / ".private" / "web_material_gateway_attestation.key").unlink()
    assert not verify_page_requirement_summary(other, artifact)

    first_contract = tmp_path / "01_page_contracts/page_001.json"
    changed = json.loads(first_contract.read_text(encoding="utf-8"))
    changed["page_comments"][0]["text"] = "改用摄影表达"
    first_contract.write_text(json.dumps(changed, ensure_ascii=False), encoding="utf-8")
    lock_path = tmp_path / "01_page_contracts/source_lock.json"
    lock = json.loads(lock_path.read_text(encoding="utf-8"))
    lock["pages"][0]["contract_sha256"] = hashlib.sha256(first_contract.read_bytes()).hexdigest()
    lock_path.write_text(json.dumps(lock), encoding="utf-8")
    assert not verify_page_requirement_summary(tmp_path, artifact)
    with pytest.raises(ValueError, match="seal|closure"):
        public_requirement_summary(tmp_path, artifact)
