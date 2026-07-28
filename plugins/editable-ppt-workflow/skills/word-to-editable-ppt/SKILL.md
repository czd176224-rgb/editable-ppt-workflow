---
name: word-to-editable-ppt
description: Convert one paginated Word document into an editable PowerPoint through one live browser style confirmation, independent gpt-image-2 page generation, relaxed page-local QA, editable reconstruction, and mechanical assembly.
---

# Word to Editable PPT

## Use this skill when

The user supplies one `.docx` and wants one editable PPT slide for each Word page. The Word document is the only required upload. Do not request a logo, reference image, master deck, sample page, or separately simplified content.

The current workflow contract is `word-only-v1`. Project files and cache remain inside the current project; they are not cross-project memory.

## Fixed workflow

`one Word → lock pagination/content/assets → three-step style-contract UI → freeze compact style contract → independent Image2 pages → relaxed page-local QA/repair → independent editable reconstruction → locked-order mechanical assembly`

There is exactly one human confirmation. There is no five-master approval and no representative-page approval.

## 1. Prepare and lock

```powershell
python scripts\word_to_editable_ppt.py prepare --word D:\Input\source.docx --output D:\Projects\Deck
python scripts\word_to_editable_ppt.py workflow next --project D:\Projects\Deck
```

Pagination rules:

1. Prefer ordered and consecutive `第1页`, `第2页` markers.
2. Only when no marker exists, use physical pages: Microsoft Word first, LibreOffice as an optional fallback.
3. Invalid, duplicated, skipped, or reordered markers are errors; never hide them with physical fallback.
4. Lock page count, order, exact page source text, source hash, tables, explicit logic, and page-local Word assets.
5. Produce exactly one slide per locked Word page.

Inline Word images and attached sources are bound only to their page. Supported images become page-local Image2 references. PDF, spreadsheet, presentation, and document attachments are marked for page-local content extraction. An unreadable attachment produces a non-blocking page advisory; it never leaks into another page.

## 2. Run the three-step visual-contract confirmation

```powershell
python scripts\word_to_editable_ppt.py confirm-ui start --project D:\Projects\Deck
python scripts\word_to_editable_ppt.py confirm-ui wait --project D:\Projects\Deck --stage final
python scripts\word_to_editable_ppt.py confirm-ui shutdown --project D:\Projects\Deck
```

The UI uses three reversible steps and still has exactly one final confirmation:

1. Select one complete template baseline: policy/project progress briefing, brand-narrative business presentation, or evidence-led investment BP. Investment BP then selects dark-tech or white-R&D visual ground.
2. Adjust template-filled details in a professional three-column visual console. Group navigation, a contextual rule specimen, and a sticky current-style summary explain every choice; there is no central fake slide preview.
3. Review the readable visual specification, select one production/delivery profile, go back if needed, and confirm once.

Template selection fills the complete visual system rather than only changing colors. It exposes:

- three scenario templates and two evidence-investment-BP substyles;
- background system, image role and proportion, evidence strength, composition tendency, and brand-device strength;
- multi-select ordered page-organization preferences (`auto`, editorial, conclusion-first, split, table, matrix, data-led, timeline, modular);
- complete semantic color controls with RGB and HEX values for background, title, section title, body, key number, accents, table header, and borders;
- separate CJK/Latin heading/body fonts, four-role point sizes, and information density;
- icon and image language;
- formal, modern, and minimal degrees;
- optional regional expression, disabled by default;
- natural-language requirements.

Every option must state its expected page effect. Every submitted visual control and every template override must appear in the confirmation receipt and executable contract. Page count, pagination mode, and one-page-to-one-slide are read-only. The production profiles are explicit and non-visual: quality = high quality / concurrency 2 / repair budget 2; balanced = high quality / concurrency 4 / repair budget 1; speed = medium quality / concurrency 6 / repair budget 1. Every profile keeps continuous page-independent production, isolated failure retry, strict project-local cache, editable PPT output, and page-image delivery.

Exact confirmed palette roles and typography are hard visual anchors. The template identity, background system, evidence strength, composition tendency, brand device, layout preferences, visual language, icon/image language, density, regional expression, and style axes remain soft preferences so Image2 retains page-specific composition freedom.

Freeze the final result with `style_contract.freeze_style_contract(project)`. It creates:

- `02_style/style_confirmation.json`: complete confirmation receipt;
- `02_style/style_execution.json`: compact executable visual contract;
- `02_style/style_execution.sha256`: immutable identity;
- `02_style/ui_preview_audit.png` and hash: project-local audit evidence only.

The UI audit PNG must never be sent to Image2, included in a page prompt, used as a reference image, or included in the page cache key.

## 3. Generate each page independently

Use `workflow next`, then claim and record the returned page request. A normal uncached page makes one `gpt-image-2` generation call through the bundled `codex-gpt-image` skill.

The initial Image2 request contains only:

- one compiled prompt containing the exact current-page Word text;
- the compact hard constraints and soft visual preferences;
- necessary images embedded on that Word page;
- model, no-crop canvas size, quality, output, and trace parameters.

It contains no UI audit image, other page, global layout plan, master/sample image, logo, QA rubric, or cross-page comparison. Preserve all source information and logical relationships, but allow Image2 to restructure it into concise, consulting-quality visual communication and independently choose the best page-specific layout.

`ppt169` maps to `1792x1008` and a `13.333333 × 7.5 in` slide. `ppt43` maps to `1536x1152` and a `10 × 7.5 in` slide. Both forbid crop or stretch.

Pages run independently under adaptive concurrency. A failed page enters its own retry path and does not block unrelated pages.

## 4. Apply relaxed page-local QA

Read the generated page once and make only two qualitative decisions:

1. Does its overall visual style match the confirmed choices?
2. Does it match this Word page's content, key facts, numbers, entities, qualifications, and logic?

Return `pass`, `pass_with_advisory`, or `repair`, with `none`, `local`, or `structural` scope. Valid paraphrase, restructuring, different layout, and small color or typography variance pass or receive an advisory. Do not run deck-global visual consistency QA.

Wrong facts/numbers/dates/entities, omitted core meaning, invented conclusions, reversed major logic, unreadable crop/overlap, or material style mismatch require repair. Local defects use `images/edits`; structural/content/logic failures use fresh generation. Repair feedback must be concise and issue-specific.

## 5. Reconstruct and cache immediately

As soon as a page passes QA, send its accepted image to `image-to-editable-ppt` for object-level reconstruction. Do not wait for all page images first.

The reconstruction attempt must be claimed before it is recorded:

```powershell
python scripts\word_to_editable_ppt.py workflow dispatch --project D:\Projects\Deck --page 1 --agent page-1 --attempt 2
editppt run record D:\Projects\Deck --page 1 --agent-id page-1 --attempt 2 --pptx D:\Projects\Deck\07_editable\page_001.pptx --artifact D:\Projects\Deck\07_editable\page_001.json
```

The strict project-local page cache identity contains:

- current-page source hash;
- compact style-contract hash;
- selected page-asset hashes and derivation parameters;
- generation parameters;
- repair feedback;
- editable reconstruction version.

A strict match reuses the completed page package. Changing page 37 or one of its attachments invalidates page 37 only. The UI audit image is excluded.

## 6. Assemble mechanically

After every locked page is complete, `image-to-editable-ppt` assembles the page packages with one writer in locked Word order. Validate page count/order, artifact hashes, editable objects, slide size, package structure, and openability. Compare layout relationships semantically; XML whitespace, quote style, or serialization changes are not visual failures.

PowerPoint is the preferred renderer. LibreOffice is an optional physical-pagination/back-render fallback, not a mandatory installation. If neither renderer exists, complete structural validation and record a non-blocking render advisory.

`officecli` may validate or surgically repair the assembled deliverable. It does not generate page images or own assembly.

## Skill ownership

| Skill | Responsibility |
| --- | --- |
| `word-to-editable-ppt` | Word pagination/content lock, page-asset binding, three-step visual-contract UI, compact contract, scheduler, relaxed page QA, and orchestration. |
| `codex-gpt-image` | One initial Image2 call per uncached page and issue-specific image repair through Codex OAuth. |
| `image-to-editable-ppt` | Object-level editable reconstruction, page-package validation/cache, and locked-order assembly. |
| `officecli` | Optional final Office validation and narrowly scoped repair. |

## Stop conditions

Stop only for invalid pagination, unavailable physical pagination for an unmarked document, empty locked page, changed frozen contract, invalid lease, project-external artifact, failed editable package, or final page-count/order mismatch. Do not stop for small fonts, high information density, a missing optional renderer, minor style variance, or one page needing automatic repair.
