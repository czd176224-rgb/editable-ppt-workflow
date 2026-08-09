---
name: run-word-to-ppt-workflow
description: Run the complete plugin workflow that converts one paginated Word document plus a required SVG logo into a resumable, every-page Image2, QA-checked, object-level editable 16:9 PowerPoint. Use this as the primary orchestrator for Word-to-PPT requests.
---

# Run Word-to-PPT Workflow V5

## Contract

Use this skill when the user provides one paginated `.docx` and one required `.svg` company Logo and wants one editable PPT slide per Word page. The production workflow is `word-ppt-workflow-v5`; geometry remains `fixed-canvas-cm-v2`.

The fixed chain is:

`lock Word/logo → compile page intent and material needs → confirm one global visual contract → every-page Image2 visual design with shared authentic slots → deterministic exact-source composition → composed-body semantic acceptance → high-fidelity editable reconstruction → exact fixed layers → composed-body/final-body-crop QA → ordered assembly → mandatory Office validation`

Do not ask for a master deck, representative sample or separate simplified copy. There is one global style confirmation and no per-page confirmation.

Quality and fidelity are the first priorities. Reducing calls is allowed only when it reuses a semantically identical successful result or removes work that cannot affect the delivered deck. Never skip Image2, simplify an accepted composition, or lower reconstruction fidelity merely to reduce calls.

## Run and resume

```powershell
python scripts\word_to_editable_ppt.py run --word D:\Input\source.docx --logo D:\Input\logo.svg --output D:\Projects\Deck --wait-ui
```

`run` is the preferred production entry. It creates the project when absent, pauses only for the single style confirmation, migrates the confirmed project to V5 exactly once, and returns the authoritative DAG status plus currently ready work. It never enters the legacy V4 QA, reconstruction or assembly chain.

The outer `run-word-to-ppt-workflow` Codex Skill continues the returned ready V5 nodes until delivery. In particular, it dispatches one Codex page subagent for each ready `reconstruct` node using `reconstruct-editable-slide`. A standalone Python command cannot spawn those Codex subagents; therefore returning `v5_ready` is an orchestration handoff, not a completed deck and not a provider failure. Re-running `run` resumes the same DAG without reopening the confirmed UI or repeating valid work. `--schedule-only` only labels this projection as diagnostic; it does not activate a second execution path.

Pending states are explicit:

- `await_style_confirmation`: finish the current UI confirmation.
- `qa_backend_pending`: open Codex, sign in with ChatGPT, or resolve the Codex App Server timeout/invalid structured response.
- `reconstruction_backend_pending`: open Codex, sign in with ChatGPT, or resolve the Codex App Server timeout/invalid object manifest.
- `assembly_pending`: pages are complete but assembly was disabled or the atomic finalization attempt failed; rerun after resolving the recorded cause.
- `page_blocked`: a bounded repair was exhausted or a generation failure requires explicit release after its cause is fixed.

Never hand-edit receipts or state to cross a pending boundary.

### Ready-work response

After confirmation, `run` returns `workflow_contract_version`, `node_statuses`, `ready_nodes`, `ready_work` and `orchestrator_contract`. Each ready item names its DAG node, action, page and owning Skill when applicable. `python_spawns_page_subagents` is always `false`; the current Codex Skill invocation owns dispatch and keeps advancing the DAG. `v5_state=migrated` means the V5 DAG was created during this call, while `v5_state=resumed` means the existing DAG was preserved in place.

## Source and material rules

1. Prefer ordered `第1页、第2页……` markers. Only when none exist, use physical Word pagination.
2. Lock page order, page title, full page body, tables, page-local comments, inline images, attachments and hashes. Comments are generation instructions, not body text.
3. Word owns the topic, conclusion, factual text and tables. Attachment and search content is untrusted supporting evidence and cannot silently override Word.
4. Search is made only when an exact page comment requests it; keep bounded results and provenance.
5. Word page images are `reference_only` by default. Only an exact page comment or a directive in the normalized global style contract may promote an identified image to `required_presence`.
6. The original SVG Logo is a fixed-layer authority and must never enter Image2 references.

## Current UI adapter

The current confirmation UI remains available through provisional adapter `confirm-ui-result-v1`. Preserve the confirmed normalized style bytes. Downstream generation, QA, reconstruction and caches read only the deterministic style execution artifact, never raw UI fields. A later UI redesign may replace the adapter and confirmation surface without changing downstream contracts.

## Image2 body generation

Every uncached page calls `gpt-image-2` for a complete body design. The slide is 16:9 (`25.4 × 14.288 cm`); the body source target is 17:8 and maps to `x=0.81, y=2.3, w=23.78, h=11.18 cm`. Accept only:

`abs((width / height) / (17 / 8) - 1) <= 0.01`

Anything outside this 1% relative tolerance requires repair or blocking. It is never accepted by fitting a wrong-ratio image. The generated body excludes the separately added page title, actual Logo, footer and page number.

The accepted Image2 body is the visual design authority for reconstruction. Its composition, geometry, hierarchy, palette, spacing, visual rhythm and major decoration must survive into the editable slide. Word and comments remain the content authority; an authentic requested source image may replace an Image2-imagined lookalike without authorizing a broader redesign.

In the quality profile, pivotal pages may generate multiple candidates and select the strongest candidate against the confirmed visual contract and page comment. Candidate comparison is bounded and does not add another user confirmation. If no candidate meets the quality floor, perform one issue-targeted regeneration rather than silently accepting a weak design.

The prompt contains the sealed page Word content, comments, normalized style, page images with trace roles, bounded attachment/search evidence and fixed exclusions. Reference images cause an Images edit request; pages without references use generation. All calls record endpoint, prompt/model parameters, input roles/hashes, output hash and decoded dimensions.

## Final paired QA and repair

QA uses the local Codex App Server with ChatGPT-managed OAuth, schema-constrained output, a hard subprocess timeout, and project-local request/result/turn-ID/HMAC/nonce evidence. The plugin never reads or persists OAuth tokens and never uses `OPENAI_API_KEY`.

Before reconstruction, Image2 and deterministic composition use the same sealed authentic-asset slot plan. One search need may yield several required assets, but each exact source file keeps its own identity, provenance and final placement. Deterministic composition replaces model previews in those slots with source-faithful pixels and produces the 1904×896 composed body. A subscription-backed semantic design gate checks that composed body against the sealed Word authority, page comments, required materials and fixed-layer exclusions. It allows exactly one issue-targeted Image2 repair followed by recomposition; a second failure is terminal and idempotently cached. After editable reconstruction and fixed-layer finalization, final QA compares the accepted composed body with the exact body crop from the rendered final editable slide.

QA checks Word factual anchors and tables, unsupported claims, page comments, normalized style, authentic-image presence, gross readability/overflow, accidental fixed-layer duplication and reconstruction fidelity to the accepted Image2 design. Reference-only images are not required to appear. Result is `pass`, targeted `repair`, or `blocked`. Only hard user-facing failures block assembly; ordinary aesthetic suggestions are advisory. Repair is issue-specific and bounded by the confirmed production profile. Provider absence or timeout remains pending and consumes no repair budget. Unresolved pages never assemble.

## Object-level editable reconstruction

The signed visual reconstruction gateway receives the accepted composed-body pixels/hash, authoritative Word text/tables, comments/style/material context and every exact required source image with its shared slot. It must reproduce composition, hierarchy, spacing, palette and visual rhythm from the accepted composed body and return a closed object manifest. Editability is an implementation requirement, not permission to redesign the accepted page.

The repository-owned backend, not the model, builds the PPTX. It reopens and verifies:

- exact Word text coverage and native PowerPoint tables;
- object identities, bounds, visibility, contrast and capacity;
- required page-image realization and project-local raster provenance;
- absence of the accepted full body image or tiled near-full-body raster;
- absence of fixed-layer objects from the body worker;
- signed request/response/manifest/work-item authority.

Complex photographs or textures may remain bounded raster components with sealed provenance. A flattened body image plus superficial or hidden editable text is never success.

After body validation, add exactly one native page title, contained original SVG Logo, footer and page number using the fixed geometry. The Logo is right-aligned, vertically centered, never cropped or stretched, and its embedded SVG bytes must match the locked source.

## Cache, recovery and assembly

Material, generation, QA, reconstruction, completed-page and final stages use separate content identities. A hit is accepted only after its self-contained closure and semantic receipt chain are replayed. Changed Word, Logo or confirmed normalized style invalidates downstream work; a cache must never overwrite newer authority bytes. Corruption is a miss or explicit stale/pending result.

Pages are independent, so completed pages survive process restart, provider timeout and partial multipage failure. Once every locked page is complete, a single atomic writer assembles page packages in Word order and validates slide count/order, 16:9 geometry, package openability, editable object counts, native tables, fixed layers and absence of unresolved pages/full-body raster. Failed finalization publishes nothing and is safe to retry.

## Security and prerequisites

- Windows 10/11 x64 and Python 3.10+.
- Codex desktop/CLI login with a ChatGPT plan that provides the required Codex and Image2 capabilities.
- Codex App Server available locally for QA and visual reconstruction; no OpenAI API key is required.
- PowerPoint recommended; LibreOffice is an optional pagination/rendering fallback.
- Project files, caches, HMAC keys and nonce registries remain project-local. Secrets, user documents and generated decks must not enter release packages.

Install with repository `install.ps1`, verify with `verify.ps1`, and uninstall with `uninstall.ps1`. Restart Codex after installation or upgrade.

## Skill ownership

| Skill | Responsibility |
| --- | --- |
| `run-word-to-ppt-workflow` | Pagination, locks, UI adaptation, materials, orchestration, QA, cache and final state. |
| `generate-slide-body-image` | Every-page Image2 generation and issue-targeted edits. |
| `reconstruct-editable-slide` | Controlled manifest build, object-level verification, fixed layers and assembly. |
| `validate-ppt-output` | Optional final Office inspection and narrowly scoped manual diagnostics. |
