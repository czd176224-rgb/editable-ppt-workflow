from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from natural_comment_resolver import resolve_comment_deterministically, resolve_page_comments  # noqa: E402
from page_material_bundle_v4 import _resolved_comment_projections, _resolved_search_directives  # noqa: E402
from page_material_bundle_v4 import SearchLimits, _search_evidence  # noqa: E402
from prompt_compiler import _validate_enterprise_logo_chain  # noqa: E402
import page_material_bundle_v4 as material_module  # noqa: E402
from page_requirement_summary import _closed_record, _resolved_directive, _validate_closed_record  # noqa: E402
from effective_page_authority import build_effective_page_authority, verify_effective_page_authority_seal  # noqa: E402
from style_contract import compile_style_execution  # noqa: E402
from test_style_contract import confirmed_result  # noqa: E402


COMPANIES = ["软通动力", "安世亚太", "中科闻歌", "星河动力", "中科亿海微", "苍穹数码科技"]


def _style_execution() -> dict:
    return compile_style_execution(confirmed_result())


def _chain():
    directive = resolve_comment_deterministically(
        "这页的企业Logo都要添加",
        {
            "page_title": "重点企业",
            "body_text": "、".join(COMPANIES),
            "source_text": "重点企业\n" + "、".join(COMPANIES),
        },
        source_comment_id="logos",
    )
    assert directive is not None
    required = [
        {
            "directive_id": item.directive_id,
            "material_id": item.material_id,
            "entity": item.entity,
            "material_role": "enterprise_logo",
        }
        for item in directive.search_requests
    ]
    evidence = [
        {
            "directive_id": item.directive_id,
            "asset_id": item.material_id,
            "entity": item.entity,
            "material_role": "enterprise_logo",
            "matched_entities": [item.entity],
            "presence_policy": "required_presence",
            "sha256": f"{index:064x}",
        }
        for index, item in enumerate(directive.search_requests, 1)
    ]
    return directive, required, evidence


def test_six_enterprise_logos_flatten_to_six_independent_searches() -> None:
    directive, required, evidence = _chain()
    intents, _page_images, queries = _resolved_comment_projections([directive])
    requests = _resolved_search_directives([directive])

    assert len(intents) == len(queries) == len(requests) == 6
    assert [item["material_id"] for item in intents] == [item.material_id for item in requests]
    assert len({item.directive_id for item in requests}) == 6
    _validate_enterprise_logo_chain(
        {"provenance": {"logo_sha256": "f" * 64}}, required, evidence,
    )


def test_material_search_seals_one_unique_authenticated_logo_per_entity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    directive, _required, _evidence = _chain()
    requests = list(directive.search_requests)
    calls = []

    def fake_search(_project, *, directives, timeout, **_kwargs):
        calls.append(([item.directive_id for item in directives], timeout))
        return [[{
            "evidence_id": f"evidence-{index}", "sha256": f"{index:064x}",
            "source_page_url": f"https://example.com/{index}", "caption": item.entity,
            "retrieved_at": "2026-08-03T00:00:00Z", "direct_image_url": f"https://cdn.example.com/{index}.png",
            "final_image_url": f"https://cdn.example.com/{index}.png", "title": item.entity,
            "publisher": "official", "local_path": f"04_v4/materials/{index}.png",
            "media_type": "image/png", "width": 200, "height": 100,
            "matched_entities": [item.entity], "material_attestation_path": f"a/{index}.json",
            "material_attestation_sha256": "a" * 64, "material_attestation_digest": "b" * 64,
            "material_attestation_signature": "c" * 64,
        }] for index, item in enumerate(directives, 1)]

    monkeypatch.setattr(material_module, "search_visual_materials", fake_search)
    monkeypatch.setattr(material_module, "verify_search_material", lambda _project, item, **_kwargs: item)
    records = _search_evidence(
        [item.query for item in requests], provider=None, limits=SearchLimits(),
        project=tmp_path, page_context={"page_number": 1}, search_directives=requests,
        fixed_logo_sha256="f" * 64,
    )

    assert len(records) == 6
    assert calls == [([item.directive_id for item in requests], 300.0)]
    assert [(item["directive_id"], item["asset_id"], item["entity"]) for item in records] == [
        (item.directive_id, item.material_id, item.entity) for item in requests
    ]


def test_enterprise_batch_uses_an_independent_timeout_for_each_post_search_verification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    directive, _required, _evidence = _chain()
    requests = list(directive.search_requests)
    clock = [0.0]
    calls: list[tuple[float, int]] = []
    verification_deadlines: list[float] = []

    def fake_search(_project, *, directives, timeout, **_kwargs):
        calls.append((timeout, len(directives)))
        clock[0] += 100.0
        return [[{
            "evidence_id": f"evidence-{index}", "sha256": f"{index:064x}",
            "source_page_url": f"https://example.com/{index}", "caption": item.entity,
            "retrieved_at": "2026-08-03T00:00:00Z", "direct_image_url": f"https://cdn.example.com/{index}.png",
            "final_image_url": f"https://cdn.example.com/{index}.png", "title": item.entity,
            "publisher": "official", "local_path": f"04_v4/materials/{index}.png",
            "media_type": "image/png", "width": 200, "height": 100,
            "matched_entities": [item.entity], "material_attestation_path": f"a/{index}.json",
            "material_attestation_sha256": "a" * 64, "material_attestation_digest": "b" * 64,
            "material_attestation_signature": "c" * 64,
        }] for index, item in enumerate(directives, 1)]

    monkeypatch.setattr(material_module.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(material_module, "search_visual_materials", fake_search)

    def fake_verify(_project, item, *, deadline, **_kwargs):
        verification_deadlines.append(deadline)
        clock[0] += 30.0
        return item

    monkeypatch.setattr(material_module, "verify_search_material", fake_verify)

    _search_evidence(
        [item.query for item in requests], provider=None, limits=SearchLimits(),
        project=tmp_path, page_context={"page_number": 4}, search_directives=requests,
        timeout=120.0, fixed_logo_sha256="f" * 64,
    )

    assert calls == [(120.0, 6)]
    assert verification_deadlines == [220.0, 250.0, 280.0, 310.0, 340.0, 370.0]


def test_transient_app_server_timeout_retries_the_same_search_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    directive, _required, _evidence = _chain()
    requests = list(directive.search_requests)
    clock = [0.0]
    calls: list[tuple[float, object]] = []

    def fake_search(_project, *, directives, timeout, budget, **_kwargs):
        calls.append((timeout, budget))
        if len(calls) == 1:
            clock[0] = 120.0
            raise material_module.SearchMaterialBlocked(
                "required visual material search failed: Codex App Server timeout",
                code="codex_app_server_timeout",
            )
        return [[{
            "evidence_id": f"evidence-{index}", "sha256": f"{index:064x}",
            "source_page_url": f"https://example.com/{index}", "caption": item.entity,
            "retrieved_at": "2026-08-03T00:00:00Z", "direct_image_url": f"https://cdn.example.com/{index}.png",
            "final_image_url": f"https://cdn.example.com/{index}.png", "title": item.entity,
            "publisher": "official", "local_path": f"04_v4/materials/{index}.png",
            "media_type": "image/png", "width": 200, "height": 100,
            "matched_entities": [item.entity], "material_attestation_path": f"a/{index}.json",
            "material_attestation_sha256": "a" * 64, "material_attestation_digest": "b" * 64,
            "material_attestation_signature": "c" * 64,
        }] for index, item in enumerate(directives, 1)]

    monkeypatch.setattr(material_module.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(material_module, "search_visual_materials", fake_search)
    monkeypatch.setattr(material_module, "verify_search_material", lambda _project, item, **_kwargs: item)

    records = _search_evidence(
        [item.query for item in requests], provider=None, limits=SearchLimits(),
        project=tmp_path, page_context={"page_number": 4}, search_directives=requests,
        timeout=120.0, fixed_logo_sha256="f" * 64,
    )

    assert len(records) == 6
    assert [item[0] for item in calls] == [120.0, 120.0]
    assert calls[0][1] is not calls[1][1]
    assert len(calls) == 2


def test_two_app_server_timeouts_stop_after_the_second_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    directive, _required, _evidence = _chain()
    request = directive.search_requests[0]
    calls = 0

    def always_times_out(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        raise material_module.SearchMaterialBlocked(
            "required visual material search failed: Codex App Server timeout",
            code="codex_app_server_timeout",
        )

    monkeypatch.setattr(material_module, "search_visual_materials", always_times_out)

    with pytest.raises(material_module.SearchMaterialBlocked, match="App Server timeout"):
        _search_evidence(
            [request.query], provider=None, limits=SearchLimits(), project=tmp_path,
            page_context={"page_number": 4}, search_directives=[request], timeout=120.0,
            fixed_logo_sha256="f" * 64,
        )

    assert calls == 2


def test_explicit_material_search_cancellation_is_not_retried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    directive, _required, _evidence = _chain()
    request = directive.search_requests[0]
    calls = 0

    def cancelled(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        raise material_module.SearchMaterialBlocked(
            "material search cancelled", code="material_search_cancelled", state="cancelled",
        )

    monkeypatch.setattr(material_module, "search_visual_materials", cancelled)
    with pytest.raises(material_module.SearchMaterialBlocked) as captured:
        _search_evidence(
            [request.query], provider=None, limits=SearchLimits(), project=tmp_path,
            page_context={"page_number": 4}, search_directives=[request], timeout=120.0,
            fixed_logo_sha256="f" * 64,
        )

    assert captured.value.code == "material_search_cancelled"
    assert captured.value.state == "cancelled"
    assert calls == 1


def test_one_word_comment_receipt_round_trips_six_child_searches(tmp_path: Path) -> None:
    context = {
        "page_title": "重点企业", "body_text": "、".join(COMPANIES),
        "source_text": "重点企业\n" + "、".join(COMPANIES),
        "page_comments": [{"comment_id": "logos", "text": "这页的企业Logo都要添加"}],
    }
    parent = resolve_page_comments(tmp_path, context, [], 5)[0]
    record = _closed_record(parent)
    _validate_closed_record(record)
    restored = _resolved_directive(record)

    assert len(restored.search_requests) == 6
    assert restored.authority_directive() == parent.authority_directive()
    assert restored.resolution_receipt == parent.resolution_receipt


def test_enterprise_directives_validate_in_standalone_authority_schema() -> None:
    parent, _required, evidence = _chain()
    authority = build_effective_page_authority(
        page_contract={"page_number": 4, "body_text": "locked body", "source_tables": []},
        style_execution=_style_execution(),
        directives=[parent.authority_directive()], page_images=[], attachment_evidence=[],
        search_evidence=evidence,
    )

    assert authority["readiness"]["status"] == "ready"
    assert verify_effective_page_authority_seal(authority)


def test_qa_required_directive_schema_accepts_only_closed_enterprise_identity() -> None:
    parent, _required, _evidence = _chain()
    enterprise = build_effective_page_authority(
        page_contract={"page_number": 4, "body_text": "locked body", "source_tables": []},
        style_execution=_style_execution(),
        directives=[parent.authority_directive()], page_images=[], attachment_evidence=[],
        search_evidence=[{"asset_id": item.material_id} for item in parent.search_requests],
    )["required_directives"][0]
    schema = json.loads(
        (SCRIPTS.parent / "schemas/page_qa_work_item_v4.schema.json").read_text(encoding="utf-8")
    )["$defs"]["requiredDirective"]
    validator = Draft202012Validator(schema)

    assert not list(validator.iter_errors(enterprise))
    assert list(validator.iter_errors({**enterprise, "unexpected": True}))


@pytest.mark.parametrize("tamper", ["missing", "duplicate_pixels", "wrong_entity", "fixed_logo"])
def test_enterprise_logo_chain_rejects_incomplete_or_misbound_material(tamper: str) -> None:
    _directive, required, evidence = _chain()
    fixed_hash = "f" * 64
    if tamper == "missing":
        evidence.pop()
    elif tamper == "duplicate_pixels":
        evidence[-1]["sha256"] = evidence[0]["sha256"]
    elif tamper == "wrong_entity":
        evidence[-1]["matched_entities"] = [COMPANIES[0]]
    else:
        evidence[-1]["sha256"] = fixed_hash

    with pytest.raises(ValueError):
        _validate_enterprise_logo_chain(
            {"provenance": {"logo_sha256": fixed_hash}}, required, copy.deepcopy(evidence),
        )
