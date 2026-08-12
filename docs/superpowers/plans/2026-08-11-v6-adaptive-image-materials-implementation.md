# V6 Adaptive Image Materials Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task by task with review checkpoints.

**Goal:** Upgrade `editable-ppt-workflow` from 2.0.3 to 2.1.0 so a new V6 project exposes the exact per-page Image2 materials in the existing one-time UI confirmation, uses `generate` without confirmed images and `edit` with confirmed real images, preserves the fixed-title/SVG-Logo/footer boundary, and produces stable, auditable results with bounded cost and latency.

**Architecture:** Keep the existing V6 source → Confirm UI → Image2 → light QA → editable reconstruction → fixed layers → assembly pipeline. Add a page-material authority between source parsing and UI, a secure reference-media service for original/thumbnail/model-input variants, and an immutable confirmed revision consumed directly by a single adaptive Image2 request builder. Search remains an orchestrator action recorded through import/failure commands; the Python runtime never performs autonomous web search or reinterprets comments after confirmation.

**Tech Stack:** Python 3.11/3.12, Flask Confirm UI, vanilla JavaScript/CSS, Pillow, defused XML/CairoSVG or the existing safe SVG rasterizer, JSON Schema, pytest, PowerPoint/OpenXML reconstruction runtime, `codex_gpt_image.py`, PowerShell release scripts.

## Global Constraints

- Only new projects created from the original paginated Word and SVG Logo are supported. Do not add V4/V5 runtime migration or fallback.
- Preserve the 25.4 × 14.288 cm 16:9 slide and the 23.78 × 11.18 cm / 1904 × 896 17:8 body region.
- The fixed page title, original SVG Logo, footer, and page number are PPT layers and must never be requested from Image2.
- The user completes the existing UI flow once. The final stage gains editable page materials; no second image-approval UI is added.
- After final UI submission, generation code reads only the frozen confirmed revision. It must not parse comments, extract attachments, search, alter facts, summarize, truncate, or invent content.
- Operation is derived only from readable confirmed model-input images: zero means `generate`; one or more means `edit`.
- `generate` has zero `--image` arguments. `edit` has 1–16 `--image` arguments and a matching `--image-role` for every input.
- Every retry is a fresh call with the same operation and original confirmed references. A previous candidate is never an input image.
- Maximum valid candidates per page is two. The first passing candidate stops generation. A second candidate requires new actionable QA feedback.
- Default concurrency is two; speed mode is three. A 429 reduces active concurrency to one and uses jittered exponential backoff.
- Reference counts: 1–6 normal, 7–10 warning, 11–16 strong warning, more than 16 rejects submission. Never truncate silently.
- Logos and screenshots receive high-fidelity best-effort prompts, not pixel-identity promises or post-generation overlays.
- Hashes are local SHA-256 integrity evidence only. They are not shown as a business requirement and are never sent to Image2.
- All implementation work is test-driven. Run the stated failing test before each production change, then the passing test after the smallest implementation.
- Keep each PR independently reviewable and keep unrelated user changes untouched.

---

## PR 1 — Page Material Authority and One-Shot Acquisition

### Task 1: Define the confirmed page-material contracts

**Files:**

- Create: `plugins/editable-ppt-workflow/skills/run-word-to-ppt-workflow/schemas/page_materials_v6.schema.json`
- Create: `plugins/editable-ppt-workflow/skills/run-word-to-ppt-workflow/schemas/reference_image_v6.schema.json`
- Create: `plugins/editable-ppt-workflow/skills/run-word-to-ppt-workflow/scripts/workflow_v6_materials.py`
- Modify: `plugins/editable-ppt-workflow/skills/run-word-to-ppt-workflow/scripts/workflow_v6_contract.py`
- Modify: `plugins/editable-ppt-workflow/skills/run-word-to-ppt-workflow/scripts/workflow_v6_source.py`
- Test: `plugins/editable-ppt-workflow/skills/run-word-to-ppt-workflow/tests/test_workflow_v6_materials.py`
- Test: `plugins/editable-ppt-workflow/skills/run-word-to-ppt-workflow/tests/test_workflow_v6_source.py`

**Public data contract:**

```python
def new_page_materials(*, page_number: int, fixed_page_title: str,
                       word_original: str, effective_body: str) -> dict[str, Any]: ...

def validate_page_materials(value: Mapping[str, Any], *, confirmed: bool) -> None: ...

def confirmed_revision_digest(result: Mapping[str, Any]) -> str: ...
```

Each page material record must contain `page_number`, `fixed_page_title`, `word_original`, `effective_body`, `attachment_extracts`, `chart_facts`, `image_requirements`, `degradations`, and `reference_images`. Reference records must contain stable `reference_id`, `source`, `purpose`, `preservation`, crop/restyle flags, status, original/model-input/thumbnail paths, source URL where applicable, and integrity metadata.

**Steps:**

- [ ] Add schema tests proving required fields, allowed status values, relative project paths, and the 16-reference hard maximum.
- [ ] Run `python -m pytest tests/test_workflow_v6_materials.py tests/test_workflow_v6_source.py -q`; expect failures because the schemas/module and page-material files do not exist.
- [ ] Implement the two JSON schemas with `additionalProperties: false` at stable business-record boundaries.
- [ ] Implement canonical JSON serialization and local SHA-256 helpers in `workflow_v6_materials.py`; reuse `workflow_v6_contract.canonical_sha256` instead of duplicating hashing behavior.
- [ ] Extend `new_project()` with `confirmed_ui_revision`, `confirmed_ui_digest`, and `page_materials_status` fields while preserving current V6 state transitions.
- [ ] Make `initialize_v6_project()` write `02_v6/page_materials/page_NNN.json` for every page and keep existing `effective_pages`/`reference_materials` only as pre-confirmation implementation artifacts.
- [ ] Validate that fixed titles are present for identification but absent from `effective_body` when they duplicate the first Word heading.
- [ ] Re-run the focused tests and expect all to pass.
- [ ] Commit with `git commit -m "feat: add v6 page material authority"`.

### Task 2: Convert comments into the three approved pre-UI responsibilities

**Files:**

- Modify: `plugins/editable-ppt-workflow/skills/run-word-to-ppt-workflow/scripts/workflow_v6_materials.py`
- Modify: `plugins/editable-ppt-workflow/skills/run-word-to-ppt-workflow/scripts/workflow_v6_source.py`
- Reuse: `plugins/editable-ppt-workflow/skills/run-word-to-ppt-workflow/scripts/natural_comment_resolver.py`
- Test: `plugins/editable-ppt-workflow/skills/run-word-to-ppt-workflow/tests/test_workflow_v6_materials.py`
- Test: `plugins/editable-ppt-workflow/skills/run-word-to-ppt-workflow/tests/test_natural_comment_resolver.py`

**Interfaces:**

```python
@dataclass(frozen=True)
class CommentResolution:
    effective_body: str
    attachment_requirements: tuple[dict[str, Any], ...]
    image_requirements: tuple[dict[str, Any], ...]
    degradations: tuple[dict[str, Any], ...]

def resolve_page_comments(*, word_original: str, fixed_page_title: str,
                          comments: Sequence[Mapping[str, Any]]) -> CommentResolution: ...
```

**Steps:**

- [ ] Add failing cases for: a comment changing a Word fact; a comment requesting selected attachment rows; a comment requesting a real person/photo; a generic concept request; a prohibited or unavailable request; and a page with no comments.
- [ ] Assert that real-identity requests produce one-shot reference acquisition requests, while generic diagrams/icons/timelines become text-only `image_requirements`.
- [ ] Assert that raw comments never appear in the confirmed Image2 material fields and that the fixed title never appears in `effective_body` solely because it was the Word page heading.
- [ ] Run `python -m pytest tests/test_workflow_v6_materials.py tests/test_natural_comment_resolver.py -q`; expect the new classification assertions to fail.
- [ ] Implement a deterministic adapter over the existing comment resolver that emits only the three approved outputs and records unsupported clauses as editable degradations.
- [ ] Keep all comment interpretation in source preparation; do not import the resolver from UI submission or Image2 generation modules.
- [ ] Re-run the focused tests and expect all to pass.
- [ ] Commit with `git commit -m "feat: compile comments into confirmed page inputs"`.

### Task 3: Extract only relevant attachment/chart facts and record one-shot real-image acquisition

**Files:**

- Modify: `plugins/editable-ppt-workflow/skills/run-word-to-ppt-workflow/scripts/workflow_v6_materials.py`
- Modify: `plugins/editable-ppt-workflow/skills/run-word-to-ppt-workflow/scripts/workflow_v6_source.py`
- Modify: `plugins/editable-ppt-workflow/skills/run-word-to-ppt-workflow/scripts/codex_web_material_gateway.py`
- Modify: `plugins/editable-ppt-workflow/skills/run-word-to-ppt-workflow/scripts/workflow_v6_cli.py`
- Test: `plugins/editable-ppt-workflow/skills/run-word-to-ppt-workflow/tests/test_workflow_v6_materials.py`
- Test: `plugins/editable-ppt-workflow/skills/run-word-to-ppt-workflow/tests/test_codex_web_material_gateway.py`
- Test: `plugins/editable-ppt-workflow/skills/run-word-to-ppt-workflow/tests/test_workflow_v6_cli.py`

**Interfaces and CLI:**

```python
def extract_attachment_material(*, attachment: Path,
                                requirement: Mapping[str, Any]) -> dict[str, Any]: ...

def chart_to_facts(chart: Mapping[str, Any]) -> dict[str, Any]: ...

def import_reference(project: Path, *, page_number: int, request_id: str,
                     image: Path, source_url: str | None) -> dict[str, Any]: ...

def fail_reference(project: Path, *, page_number: int, request_id: str,
                   reason: str) -> dict[str, Any]: ...
```

Add commands:

```text
workflow_v6_cli.py import-reference --project P --page N --request-id R --image FILE [--source-url URL]
workflow_v6_cli.py fail-reference --project P --page N --request-id R --reason TEXT
```

**Steps:**

- [ ] Add fixtures for a long attachment where the comment selects a bounded subset, a chart converted to exact title/series/unit/value/trend relations, an unavailable attachment, and a stable extraction receipt.
- [ ] Add acquisition state tests for `pending → found → confirmed`, `pending → failed_no_retry`, `found → user_rejected`, and refusal to execute/import a second result after `failed_no_retry`.
- [ ] Add a test proving the gateway emits a bounded orchestrator work item rather than downloading from Python autonomously.
- [ ] Add a test proving `source_url` is metadata only and is never dereferenced by the Python CLI/server; this removes the SSRF surface instead of maintaining a second web downloader.
- [ ] Run the three focused test files; expect failures for missing extraction and CLI commands.
- [ ] Implement attachment extraction with a content-based receipt so a stable prior extraction is reused without reparsing.
- [ ] Convert charts to textual facts only; never put chart image paths into `reference_images`.
- [ ] Make the Codex/web gateway write one-shot requests containing page, purpose, identity/evidence need, and status. The outer skill may use web search once and then call `import-reference` or `fail-reference`.
- [ ] Ensure unavailable attachments and failed searches create editable degradation text and never block project preparation.
- [ ] Re-run focused tests and expect all to pass.
- [ ] Run `python -m pytest tests/test_workflow_v6_source.py tests/test_workflow_v6_materials.py tests/test_codex_web_material_gateway.py tests/test_workflow_v6_cli.py -q`.
- [ ] Commit with `git commit -m "feat: prepare bounded attachment and reference materials"`.

**PR 1 review gate:**

- [ ] Confirm no V4/V5 module is imported by new V6 material code.
- [ ] Confirm a fresh project reaches UI readiness with unavailable attachments/searches recorded but not blocking.
- [ ] Open PR 1 with title `V6 materials: establish confirmed page authority`.

---

## PR 2 — Secure Media and Editable Final UI Stage

### Task 4: Normalize reference images and expose project-contained media safely

**Files:**

- Create: `plugins/editable-ppt-workflow/skills/run-word-to-ppt-workflow/scripts/workflow_v6_media.py`
- Modify: `plugins/editable-ppt-workflow/skills/run-word-to-ppt-workflow/scripts/workflow_v6_materials.py`
- Modify: `plugins/editable-ppt-workflow/skills/run-word-to-ppt-workflow/scripts/confirm_ui/server.py`
- Test: `plugins/editable-ppt-workflow/skills/run-word-to-ppt-workflow/tests/test_workflow_v6_media.py`
- Test: `plugins/editable-ppt-workflow/skills/run-word-to-ppt-workflow/tests/test_confirm_ui_contract.py`

**Interfaces:**

```python
@dataclass(frozen=True)
class NormalizedReference:
    original_path: str
    thumbnail_path: str
    model_input_path: str
    original_sha256: str
    model_input_sha256: str
    mime_type: str
    width: int
    height: int

def normalize_reference(project: Path, source: Path, *, reference_id: str,
                        kind: str) -> NormalizedReference: ...

def resolve_project_media(project: Path, relative_path: str, *, variant: str) -> Path: ...
```

**Security limits:** 25 MB encoded file, 80 megapixels decoded image, 16,384 px maximum edge, safe rasterized SVG previews/model inputs, no inline serving of SVG/HTML, and no symlink/path escape.

**Steps:**

- [ ] Add failing tests for magic/MIME mismatch, corrupt files, decompression-bomb dimensions, EXIF orientation/GPS removal, aspect preservation, screenshot PNG preservation, SVG external-reference/script rejection, and project-root/symlink traversal.
- [ ] Add endpoint tests requiring the existing project identity and session nonce for `thumbnail`, `original`, and `model-input` variants.
- [ ] Run `python -m pytest tests/test_workflow_v6_media.py tests/test_confirm_ui_contract.py -q`; expect failures because normalization and media routes do not exist.
- [ ] Implement validation using decoded content rather than extensions; set Pillow bomb limits and close images deterministically.
- [ ] Preserve original bytes in the project, create a safe thumbnail, and create a cost-bounded model input. Photos lose EXIF/GPS and retain ratio; screenshots remain lossless PNG; SVG Logo is rasterized without stretch/crop.
- [ ] Reuse the existing safe SVG rasterizer if it meets these tests; otherwise isolate rasterization with external resources disabled.
- [ ] Add a nonce-protected media route that only returns validated project files with `nosniff`, a safe content type, and attachment disposition for untrusted originals.
- [ ] Re-run focused tests and expect all to pass.
- [ ] Commit with `git commit -m "feat: secure v6 reference media"`.

### Task 5: Make final-stage page materials editable and freeze one atomic revision

**Files:**

- Modify: `plugins/editable-ppt-workflow/skills/run-word-to-ppt-workflow/scripts/confirm_ui/server.py`
- Modify: `plugins/editable-ppt-workflow/skills/run-word-to-ppt-workflow/scripts/confirm_ui/static/index.html`
- Modify: `plugins/editable-ppt-workflow/skills/run-word-to-ppt-workflow/scripts/confirm_ui/static/app.js`
- Modify: `plugins/editable-ppt-workflow/skills/run-word-to-ppt-workflow/scripts/confirm_ui/static/style.css`
- Modify: `plugins/editable-ppt-workflow/skills/run-word-to-ppt-workflow/schemas/style_confirmation.schema.json`
- Modify: `plugins/editable-ppt-workflow/skills/run-word-to-ppt-workflow/scripts/workflow_v6_cli.py`
- Test: `plugins/editable-ppt-workflow/skills/run-word-to-ppt-workflow/tests/test_confirm_ui_contract.py`
- Test: `plugins/editable-ppt-workflow/skills/run-word-to-ppt-workflow/tests/test_style_contract.py`
- Test: `plugins/editable-ppt-workflow/skills/run-word-to-ppt-workflow/tests/test_workflow_v6_cli.py`

**Final result shape:**

```json
{
  "status": "confirmed",
  "revision": 1,
  "global_visual_contract": {},
  "production_profile": "balanced",
  "confirmed_pages": []
}
```

**Steps:**

- [ ] Replace read-only page-requirement assertions with tests for editable `effective_body`, attachment extracts, chart facts, image requirements, reference purpose/crop/restyle controls, and degradation language.
- [ ] Test that the UI displays Word original and fixed title for context but serializes neither as duplicate Image2 authority.
- [ ] Test thumbnail/full-original/model-input URLs and per-page reference counts/warnings: warning at 7, strong warning at 11, rejection at 17.
- [ ] Test prompt-length estimation against the final compiler overhead; reject a page that would exceed 32,000 characters rather than truncating it.
- [ ] Test an atomic first submission at revision 1, a subsequent full resubmission at revision 2, stale-revision rejection, and unchanged content preservation across server restart.
- [ ] Run the three focused test files; expect stage-3 and submission assertions to fail.
- [ ] Extend `_v6_project_pages()` to return the full editable page material records and safe media URLs.
- [ ] Update stage-3 rendering and client state so all page edits remain local until one final submission.
- [ ] Validate the complete payload server-side, write to a temporary file, `fsync`, and atomically replace `confirm_ui/result.json`.
- [ ] Store the local revision digest and each model-input digest; do not send or display them as confirmation work.
- [ ] Change `confirm-style` into sealing the complete UI result: compile the global visual contract once and persist a frozen confirmed page set into V6 state/project records.
- [ ] Ensure downstream modules have no write path to confirmed page materials.
- [ ] Re-run focused tests and expect all to pass.
- [ ] Commit with `git commit -m "feat: confirm editable per-page image materials"`.

**PR 2 review gate:**

- [ ] Manually open a local fixture project and verify safe thumbnail, full original, and actual model-input image are visibly distinct where normalization changed the file.
- [ ] Confirm one UI submission freezes both style and all page materials.
- [ ] Open PR 2 with title `Confirm UI: edit and freeze exact Image2 inputs`.

---

## PR 3 — Adaptive Image2 Requests, Cost Controls, and Recovery

### Task 6: Build one authoritative adaptive request from confirmed material only

**Files:**

- Modify: `plugins/editable-ppt-workflow/skills/run-word-to-ppt-workflow/scripts/workflow_v6_image.py`
- Modify: `plugins/editable-ppt-workflow/skills/generate-slide-body-image/scripts/codex_gpt_image.py`
- Test: `plugins/editable-ppt-workflow/skills/run-word-to-ppt-workflow/tests/test_workflow_v6_image.py`
- Test: `plugins/editable-ppt-workflow/skills/generate-slide-body-image/tests/test_generation_trace.py`

**Interfaces:**

```python
@dataclass(frozen=True)
class ImageRequest:
    operation: Literal["generate", "edit"]
    quality: Literal["medium", "high"]
    prompt: str
    input_images: tuple[Path, ...]
    image_roles: tuple[str, ...]

def build_image_request(*, confirmed_page: Mapping[str, Any],
                        visual_contract: Mapping[str, Any],
                        qa_feedback: Sequence[str] = ()) -> ImageRequest: ...

def build_image_command(request: ImageRequest, *, prompt_file: Path,
                        output: Path, trace: Path) -> list[str]: ...
```

**Steps:**

- [ ] Replace generate-only tests with a matrix: zero readable confirmed images → generate/no `--image`; one to sixteen → edit/repeated `--image` and roles; stale/unreadable records are excluded; all invalid images safely produce generate.
- [ ] Add a regression proving an edit command can never be built with zero images and a generate command can never carry images.
- [ ] Add tests proving the previous candidate path cannot appear in an edit request or retry.
- [ ] Run the two focused test files; expect failures under the existing hard-coded generate path.
- [ ] Implement `ImageRequest` and derive operation only after resolving current confirmed model-input paths.
- [ ] Generalize `build_generate_command` to `build_image_command`; call the existing CLI `generate` or `edit` subcommand and pass matching roles.
- [ ] Retain `codex_gpt_image.py` hard guards: max 16 images, edit requires at least one, generate rejects any image.
- [ ] Emit operation, exact input paths, roles, model, size, and quality in the local trace without embedding image bytes or sensitive prompt content.
- [ ] Re-run focused tests and expect all to pass.
- [ ] Commit with `git commit -m "feat: select image2 generate or edit from confirmed refs"`.

### Task 7: Enforce adaptive quality, two-candidate budget, and bounded concurrency

**Files:**

- Modify: `plugins/editable-ppt-workflow/skills/run-word-to-ppt-workflow/scripts/workflow_v6_image.py`
- Modify: `plugins/editable-ppt-workflow/skills/run-word-to-ppt-workflow/scripts/workflow_v6_cli.py`
- Modify: `plugins/editable-ppt-workflow/skills/run-word-to-ppt-workflow/scripts/adaptive_scheduler.py`
- Modify: `plugins/editable-ppt-workflow/skills/run-word-to-ppt-workflow/scripts/style_contract.py`
- Modify: `plugins/editable-ppt-workflow/skills/run-word-to-ppt-workflow/schemas/style_confirmation.schema.json`
- Test: `plugins/editable-ppt-workflow/skills/run-word-to-ppt-workflow/tests/test_workflow_v6_image.py`
- Test: `plugins/editable-ppt-workflow/skills/run-word-to-ppt-workflow/tests/test_workflow_v6_concurrency.py`
- Test: `plugins/editable-ppt-workflow/skills/run-word-to-ppt-workflow/tests/test_style_contract.py`

**Deterministic policy:**

```python
def initial_quality(page: Mapping[str, Any]) -> str:
    return "high" if has_logo_or_screenshot(page) or dense_data(page) \
        or small_text_risk(page) or high_detail_role(page) else "medium"
```

**Steps:**

- [ ] Add quality tests for ordinary low-text/photo pages (`medium`) and Logo/screenshot/dense-data/small-text/high-detail pages (`high`).
- [ ] Add candidate tests: first QA pass makes one call; actionable first failure makes at most a second call; repeated/non-actionable feedback makes no second call; medium may upgrade to high only for an actionable retry.
- [ ] Change CLI default `--max-candidates` from 3 to 2 and reject values above 2.
- [ ] Add scheduler tests for balanced/quality concurrency 2, speed concurrency 3, 429 reduction to 1, jittered exponential delay bounds, and retry only for 429/5xx/network interruption.
- [ ] Run the three focused test files; expect current defaults and retry behavior to fail.
- [ ] Implement deterministic risk classification using only frozen material types/counts/text length/chart count/purpose fields.
- [ ] Cap all paths at two valid candidates even if a caller passes a larger value.
- [ ] Implement scheduler feedback that lowers active concurrency after 429 without resetting completed page receipts.
- [ ] Normalize existing UI profiles to `balanced=2`, `quality=2`, `speed=3`; preserve size 1904×896.
- [ ] Re-run focused tests and expect all to pass.
- [ ] Commit with `git commit -m "perf: bound v6 image cost and concurrency"`.

### Task 8: Make receipts content-addressed and resumable for both operations

**Files:**

- Modify: `plugins/editable-ppt-workflow/skills/run-word-to-ppt-workflow/scripts/workflow_v6_image.py`
- Modify: `plugins/editable-ppt-workflow/skills/run-word-to-ppt-workflow/scripts/workflow_v6_contract.py`
- Test: `plugins/editable-ppt-workflow/skills/run-word-to-ppt-workflow/tests/test_workflow_v6_image.py`
- Test: `plugins/editable-ppt-workflow/skills/run-word-to-ppt-workflow/tests/test_generation_receipt_boundary.py`
- Test: `plugins/editable-ppt-workflow/skills/run-word-to-ppt-workflow/tests/test_request_ledger.py`

**Receipt identity:**

```python
def request_identity(*, revision_digest: str, prompt_sha256: str,
                     operation: str, quality: str,
                     input_sha256s: Sequence[str]) -> str: ...
```

**Steps:**

- [ ] Add resume tests for valid generate and edit receipts and invalidation tests for changed revision, prompt, operation, quality, image content at the same path, candidate file, or trace.
- [ ] Add a test proving hashes remain local and none appear in the prompt or API arguments.
- [ ] Run the three focused test files; expect edit receipt and content-change assertions to fail.
- [ ] Replace `image2-generate-v6` with an operation-neutral `image2-adaptive-v6` receipt carrying revision/prompt/input/output digests.
- [ ] Verify actual file bytes and trace semantics before reuse; do not trust path existence alone.
- [ ] Preserve the first valid candidate and return it when QA is unavailable, the second call fails, or the second result has no effective improvement.
- [ ] Re-run focused tests and expect all to pass.
- [ ] Commit with `git commit -m "feat: resume adaptive image requests safely"`.

**PR 3 review gate:**

- [ ] Inspect command traces for one generate fixture and one edit fixture.
- [ ] Confirm no edit trace lacks inputs and no retry includes candidate 1 as an input.
- [ ] Open PR 3 with title `Image2: adaptive references with bounded quality and recovery`.

---

## PR 4 — Prompt Authority and Two-Layer QA

### Task 9: Compile a lean prompt from frozen UI authority

**Files:**

- Modify: `plugins/editable-ppt-workflow/skills/run-word-to-ppt-workflow/scripts/workflow_v6_image.py`
- Create: `plugins/editable-ppt-workflow/skills/run-word-to-ppt-workflow/tests/test_workflow_v6_prompt.py`
- Modify: `plugins/editable-ppt-workflow/skills/run-word-to-ppt-workflow/tests/test_workflow_v6_image.py`

**Prompt sections, in order:**

```text
system generation constraints
global visual contract
1904x896 / 17:8 geometry and fixed-layer exclusions
confirmed effective body
confirmed attachment extracts
confirmed chart facts
confirmed image requirements
reference IDs, roles, and preservation instructions
confirmed degradation expressions
new actionable QA feedback, if any
```

**Steps:**

- [ ] Add exact-section tests proving no raw comments, duplicate Word original, fixed title, UI metadata, search process records, or backend conclusions enter the prompt.
- [ ] Add a regression for the prior duplicate-title issue and one for style-contract loss.
- [ ] Add reference-role tests stating edit inputs are independent materials, not a canvas; Logo/screenshot text/ratio should be preserved best-effort; no unreferenced real identity/brand/evidence may be fabricated.
- [ ] Add a 32,000-character boundary test and an empty-style contract test; both must fail locally before the runner is called.
- [ ] Run `python -m pytest tests/test_workflow_v6_prompt.py tests/test_workflow_v6_image.py -q`; expect failures under the current prompt builder.
- [ ] Replace prompt construction inputs with `confirmed_page` plus frozen `global_visual_contract`; remove reads of raw `effective_page`, raw comments, and legacy reference descriptions during generation.
- [ ] Serialize each authority section once and keep user text verbatim; do not summarize or silently truncate.
- [ ] Re-run focused tests and expect all to pass.
- [ ] Commit with `git commit -m "fix: make confirmed ui materials the prompt authority"`.

### Task 10: Split free mechanical QA from one semantic visual review

**Files:**

- Modify: `plugins/editable-ppt-workflow/skills/run-word-to-ppt-workflow/scripts/workflow_v6_qa.py`
- Modify: `plugins/editable-ppt-workflow/skills/run-word-to-ppt-workflow/scripts/workflow_v6_image.py`
- Create: `plugins/editable-ppt-workflow/skills/run-word-to-ppt-workflow/tests/test_workflow_v6_qa.py`
- Modify: `plugins/editable-ppt-workflow/skills/run-word-to-ppt-workflow/tests/test_workflow_v6_image.py`

**Interfaces:**

```python
def mechanical_review(*, request: ImageRequest, output: Path,
                      receipt_inputs: Mapping[str, Any]) -> dict[str, Any]: ...

def semantic_review(*, image: Path, confirmed_page: Mapping[str, Any],
                    visual_contract: Mapping[str, Any],
                    reference_roles: Sequence[str]) -> dict[str, Any]: ...

def actionable_retry_feedback(current: Mapping[str, Any],
                              previous: Mapping[str, Any] | None) -> list[str]: ...
```

**Steps:**

- [ ] Add mechanical failures for missing/undecodable/wrong-size output, operation/input mismatch, overlong prompt, empty style, and digest mismatch; prove semantic QA is not called after mechanical failure.
- [ ] Add semantic cases for confirmed-body/requirement/style mismatch, generated fixed title/Logo/footer/page number, missing recognizable required reference, invented real identity/brand/event/product, and severe Logo/screenshot deformation.
- [ ] Add exclusions proving QA does not require pixel identity, exact original hash appearance, reconstruction suitability, post-reconstruction comparison, or later overlays.
- [ ] Add retry-gate tests: only new nonempty actionable feedback allows candidate 2; repeated feedback, QA outage, or no improvement selects candidate 1.
- [ ] Run the focused QA/image tests; expect failures before the split is implemented.
- [ ] Implement local mechanical review first and invoke the semantic reviewer only for a mechanically valid candidate.
- [ ] Keep semantic review to one call per valid candidate and return concise correction strings used verbatim in a fresh request.
- [ ] Re-run focused tests and expect all to pass.
- [ ] Commit with `git commit -m "test: enforce layered v6 image qa"`.

**PR 4 review gate:**

- [ ] Review a saved prompt fixture and verify it matches the confirmed UI material exactly once.
- [ ] Confirm title/Logo/footer exclusions are checked before reconstruction and no post-reconstruction visual repair was introduced.
- [ ] Open PR 4 with title `V6 quality: authoritative prompts and layered QA`.

---

## PR 5 — Workflow Integration, Real E2E, and 2.1.0 Release

### Task 11: Wire the skill workflow and remove generate-only production claims

**Files:**

- Modify: `plugins/editable-ppt-workflow/skills/run-word-to-ppt-workflow/SKILL.md`
- Modify: `plugins/editable-ppt-workflow/skills/run-word-to-ppt-workflow/README.md`
- Modify: `plugins/editable-ppt-workflow/skills/run-word-to-ppt-workflow/agents/openai.yaml`
- Modify: `plugins/editable-ppt-workflow/skills/generate-slide-body-image/SKILL.md`
- Modify: `plugins/editable-ppt-workflow/skills/generate-slide-body-image/agents/openai.yaml`
- Modify: `plugins/editable-ppt-workflow/README.md`
- Modify: `plugins/editable-ppt-workflow/scripts/check_current_runtime.py`
- Test: `plugins/editable-ppt-workflow/skills/run-word-to-ppt-workflow/tests/test_v6_runtime_diagnostics.py`
- Test: `tests/test_skill_role_names.py`

**Steps:**

- [ ] Add documentation contract tests that require the one-time UI sequence, one-shot search import/failure recording, adaptive generate/edit rule, two-candidate cap, fixed-layer exclusions, and no post-confirmation reinterpretation.
- [ ] Add negative checks preventing active V6 instructions from claiming generate-only, reference-description-only, V4/V5 fallback, or post-generation exact overlay.
- [ ] Run `python -m pytest plugins/editable-ppt-workflow/skills/run-word-to-ppt-workflow/tests/test_v6_runtime_diagnostics.py tests/test_skill_role_names.py -q`; expect failures against current wording/metadata.
- [ ] Update the workflow instructions so orchestration completes pending real-image acquisitions once, opens the existing Confirm UI once, then consumes the frozen result automatically.
- [ ] Document that failed search is marked `failed_no_retry`, UI may accept/upload a replacement, and generation remains non-blocking.
- [ ] Update diagnostics to require the new schemas/modules and verify the Image2 CLI supports both subcommands.
- [ ] Re-run focused tests and expect all to pass.
- [ ] Commit with `git commit -m "docs: route v6 through confirmed adaptive materials"`.

### Task 12: Prove the full new-project workflow with deterministic and live gates

**Files:**

- Create: `plugins/editable-ppt-workflow/skills/run-word-to-ppt-workflow/tests/fixtures/v6_adaptive_project/fixture.json`
- Create: `plugins/editable-ppt-workflow/skills/run-word-to-ppt-workflow/tests/test_workflow_v6_adaptive_e2e.py`
- Modify: `plugins/editable-ppt-workflow/skills/run-word-to-ppt-workflow/tests/test_mixed_workflow_e2e.py`
- Modify: `plugins/editable-ppt-workflow/skills/run-word-to-ppt-workflow/tests/test_final_mechanical_assembly.py`
- Modify: `plugins/editable-ppt-workflow/skills/run-word-to-ppt-workflow/tests/test_independent_page_workflow.py`

**Four-page acceptance fixture:**

1. Text/concept page with no real image → medium generate.
2. Real meeting/person photo page → medium edit with original confirmed reference.
3. Company Logo/screenshot page → high edit with safe normalized inputs and preservation roles.
4. Chart/attachment page → confirmed textual facts, no chart image input, generate unless another confirmed image exists.

**Steps:**

- [ ] Write a fake-runner E2E test that initializes a new project, resolves comments/attachments, records one successful and one failed-no-retry acquisition, submits one UI revision, builds correct requests, exercises QA pass/retry/fallback, reconstructs pages, adds fixed layers, and assembles in Word order.
- [ ] Assert every output is 1904×896, every PPT is 16:9, the original SVG Logo is in the fixed top-right layer without stretch/crop, and the fixed title is absent from body authority/prompt.
- [ ] Assert candidate receipts, prompt/input/output digests, reference roles, states, and recovery behavior.
- [ ] Run the new E2E test first and expect failure until all PRs are integrated.
- [ ] Make only integration fixes required by the frozen design; do not add alternate branches.
- [ ] Run `python -m pytest plugins/editable-ppt-workflow/skills/run-word-to-ppt-workflow/tests/test_workflow_v6_adaptive_e2e.py plugins/editable-ppt-workflow/skills/run-word-to-ppt-workflow/tests/test_mixed_workflow_e2e.py plugins/editable-ppt-workflow/skills/run-word-to-ppt-workflow/tests/test_final_mechanical_assembly.py plugins/editable-ppt-workflow/skills/run-word-to-ppt-workflow/tests/test_independent_page_workflow.py -q`.
- [ ] Run the complete plugin suite: `python -m pytest plugins/editable-ppt-workflow/skills/run-word-to-ppt-workflow/tests plugins/editable-ppt-workflow/skills/generate-slide-body-image/tests plugins/editable-ppt-workflow/skills/reconstruct-editable-slide/cli/tests -q`.
- [ ] Run the repository suite: `python -m pytest tests -q`.
- [ ] Run syntax validation: `python scripts/check_python_syntax.py`.
- [ ] Run an authorized live smoke on the supplied Word/SVG in a brand-new project directory with one generate page and one edit page. Save prompts, input variants, traces, QA, reconstructed PPTX, preview, and validation evidence; do not modify the 2.0.3 project.
- [ ] Inspect the generated body images before reconstruction for title duplication, invented facts, missing references, and fixed-Logo generation; inspect final slides for original fixed SVG placement.
- [ ] Commit with `git commit -m "test: cover adaptive v6 workflow end to end"`.

### Task 13: Release, install, and publish 2.1.0 only after all gates pass

**Files:**

- Modify: `plugins/editable-ppt-workflow/.codex-plugin/plugin.json`
- Modify: `package-info.json`
- Modify: `.agents/plugins/marketplace.json`
- Modify: `README.md`
- Modify: `docs/RELEASE.md`
- Modify: `docs/USER_GUIDE.zh-CN.md`
- Modify: `docs/TROUBLESHOOTING.zh-CN.md`
- Regenerate: `public-release-audit.json`
- Regenerate: `public-release-files.json`
- Regenerate: `public-source-manifest.json`
- Test: `tests/test_public_distribution.py`
- Test: `tests/test_release_hardening_v2.py`
- Test: `tests/test_install_receipt_state_machine.py`

**Release metadata:**

```text
pluginVersion = 2.1.0
releaseTag = v2.1.0
workflowContractVersion = word-ppt-workflow-v6
promptContractVersion = page-prompt-v6-adaptive-confirmed-materials
pageImagePolicy = generate-without-refs-edit-with-confirmed-refs
```

**Steps:**

- [ ] Add/update release tests requiring 2.1.0 metadata and adaptive image policy consistently in manifest, marketplace, package info, public audit, and docs.
- [ ] Run `python -m pytest tests/test_public_distribution.py tests/test_release_hardening_v2.py tests/test_install_receipt_state_machine.py -q`; expect failures while metadata remains 2.0.3.
- [ ] Update manifests and user documentation, including the high-fidelity-best-effort limitation and one-time UI behavior.
- [ ] Build public manifests with the existing release scripts; inspect that no project inputs, prompts, generated images, access tokens, caches, or test secrets enter the archive.
- [ ] Run `powershell -ExecutionPolicy Bypass -File scripts/release_gate.ps1` and require exit code 0.
- [ ] Run `powershell -ExecutionPolicy Bypass -File scripts/package_release.ps1` and verify the archive with `scripts/check_public_release.py`.
- [ ] Install the locally built 2.1.0 package through the repository's supported update/install path; verify the installed cache reports 2.1.0 and both generate/edit CLI behavior.
- [ ] Re-run the four-page smoke using the installed package, not the source tree, and compare receipts/validation with the source run.
- [ ] Commit with `git commit -m "release: editable ppt workflow 2.1.0"`.
- [ ] Push the five reviewed PR branches, merge in order, tag `v2.1.0`, and let `.github/workflows/release.yml` publish only after CI is green.
- [ ] Verify the public GitHub release/archive and marketplace entry resolve to 2.1.0, then perform one clean install verification.

**PR 5 review gate:**

- [ ] Use `superpowers:verification-before-completion` and cite fresh command output for every completion claim.
- [ ] Use `superpowers:requesting-code-review` for the final cross-PR audit.
- [ ] Open PR 5 with title `Release editable-ppt-workflow 2.1.0`.

---

## Final Acceptance Matrix

| Requirement | Automated evidence | Manual/live evidence |
|---|---|---|
| One UI confirmation | Confirm UI revision tests | One submission completes smoke |
| Exact confirmed material authority | Prompt and immutable-revision tests | Saved prompt matches UI fields |
| No reference → generate | Request matrix | Generate trace has no images |
| Reference → edit | Request matrix | Edit trace has original refs |
| No empty edit | CLI/request guard tests | Trace inspection |
| No previous-candidate edit | Retry tests | Candidate 2 input trace |
| Original image visible in UI | Media endpoint tests | Thumbnail/full/model-input view |
| One-shot search failure | Gateway state tests | Failed request remains failed-no-retry |
| Fixed title not duplicated | Prompt regression | Body-image inspection |
| Fixed SVG Logo is original layer | Assembly tests | Final slide inspection |
| No silent fact changes | Frozen authority tests | UI/prompt comparison |
| Bounded quality/cost | Quality/candidate tests | Receipt shows ≤2 candidates |
| Stable rate-limit recovery | Scheduler tests | Fault-injection trace |
| Secure media handling | MIME/path/SVG/bomb/SSRF tests | Public archive inspection |
| Resumable without duplicate calls | Receipt identity tests | Interrupted smoke resume |
| Editable reconstruction remains intact | Reconstruction/mechanical tests | PPT object inspection |

## Implementation Completion Checklist

- [ ] All 13 tasks were completed in order with their focused tests run red then green.
- [ ] All five PR review gates passed; no scope outside the approved design was added.
- [ ] Search/import and UI media routes reject project escapes, corrupt images, and executable SVG/HTML content; automated tests prove no Python URL-fetch endpoint exists.
- [ ] Active V6 code has no runtime imports from V4/V5 production modules.
- [ ] Active V6 instructions contain no generate-only claim and no exact-overlay promise.
- [ ] Search active source, schemas, tests, and release metadata for unfinished implementation markers and obsolete generate-only production claims; review every result and allow obsolete wording only in explicitly historical notes.
- [ ] `git status --short` is clean after the release commit.
- [ ] Fresh source-tree and installed-package E2E evidence both pass before tagging.
