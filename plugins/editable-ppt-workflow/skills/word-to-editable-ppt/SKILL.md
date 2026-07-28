---
name: word-to-editable-ppt
description: Convert one paginated Word document into an editable PPT through one embedded three-stage browser confirmation phase, independent page image generation, relaxed page-local QA, independent reconstruction, and mechanical final assembly.
---

# Word to Editable PPT Workflow

## When to use

Use this Skill when the user supplies one `.docx` and wants an editable PowerPoint with one Word page mapped to one slide. The Word document is the only required user file. Do not request any additional style, brand, reference, or representative-page upload.

The workflow contract is `word-only-v1`. It is current-only: do not open, migrate, or branch for projects created under another contract.

## Fixed flow

`one paginated Word → locked page order → one embedded three-stage browser confirmation phase → immutable shared style contract → independent page image generation → relaxed page-local QA and repair → independent editable reconstruction → mechanical final assembly`

The browser interaction is embedded in this plugin and adapted from the PPT Master interaction model. It has no import, filesystem lookup, package dependency, or separately installed Skill requirement outside this plugin.

## 1. Prepare and lock the Word pages

Run:

```powershell
python scripts\word_to_editable_ppt.py prepare --word D:\Input\source.docx --output D:\Projects\Deck
python scripts\word_to_editable_ppt.py workflow next --project D:\Projects\Deck
```

Pagination rules are strict:

1. Prefer ordered, consecutive `第1页`, `第2页`, and later markers.
2. If no marker exists anywhere, use physical pages rendered by Microsoft Word; resolve LibreOffice lazily only as an optional fallback.
3. Stop on duplicated, skipped, or out-of-order markers; never fall back from invalid markers.
4. Lock page count, page numbers, page order, page-local source text, and source hashes.
5. Produce exactly one PPT slide for each locked Word page. Do not add slides that are not represented by Word pages.

`workflow next`, `status`, and `resume` must return `await_style_confirmation` until the browser result has been finalized and frozen.

## 2. Complete the one browser confirmation phase

The browser session contains three consecutive stages but is one uninterrupted human confirmation phase.

- Stage 1 confirms audience, core message, delivery context, source-treatment latitude, and canvas. Page count, pagination mode, and one-page-to-one-slide are read-only facts.
- Stage 2 offers at least three coordinated directions and confirms purpose, page mode, overall visual style, a six-role palette, CJK/Latin font stacks, a four-role point-size scale, information density, icon and image-rendering language, formal/modern/minimal degrees, and free-form user requirements.
- Stage 3 confirms formula handling, continuous or split execution, image quality, maximum concurrency, the local-edit repair budget before full-regeneration escalation, editable output, optional specification refinement, and generation start.

`continuous` keeps filling available scheduler capacity up to `max_concurrency`. `split` uses `max_concurrency` as a deterministic batch size over locked Word-page order and does not release the next batch until every page in the current batch reaches `complete`.

The selected canvas is executable, not descriptive: `ppt169` maps to the legal `gpt-image-2` size `1792x1008` and a `13.333333 × 7.5 in` slide; `ppt43` maps to `1536x1152` and `10 × 7.5 in`. Both use `contain` and forbid cropping. The finalizer rejects reconstructed page packages whose slide size differs from the confirmed canvas.

Write each stage recommendation to `<project>/confirm_ui/recommendations.json`, then use the embedded lifecycle:

```powershell
python scripts\word_to_editable_ppt.py confirm-ui start --project D:\Projects\Deck
python scripts\word_to_editable_ppt.py confirm-ui wait --project D:\Projects\Deck --stage final
python scripts\word_to_editable_ppt.py confirm-ui shutdown --project D:\Projects\Deck
```

The final result is `<project>/confirm_ui/result.json`. Call `style_contract.freeze_style_contract(project)` immediately afterward. It writes canonical `02_style/style_confirmation.json`, `style_execution.json`, and `style_execution.sha256` and advances the sole confirmation gate. Never rewrite these files per page or after page work begins.

## 3. Generate pages independently

Each page advances through `queued`, `generating`, `qa`, `repair`, `accepted`, `reconstructing`, and `complete`. Use `workflow next` to obtain capacity-bounded page requests, then claim and record each page with:

```powershell
python scripts\word_to_editable_ppt.py workflow dispatch --project D:\Projects\Deck --page 1 --agent page-1 --attempt 1
python scripts\word_to_editable_ppt.py workflow record-generation --project D:\Projects\Deck --page 1 --agent page-1 --attempt 1 --image D:\Projects\Deck\06_images\generated\page_001_attempt_001.png
python scripts\word_to_editable_ppt.py workflow record-qa --project D:\Projects\Deck --page 1 --agent page-1 --attempt 1 --qa-file D:\Projects\Deck\08_qa\page_001.json
```

Initial generation always uses `images/generations`. Its factual input is the exact current-page text; its visual input is the exact frozen `style_execution.json`; its remaining inputs are only technical output parameters. Do not pass another page, an external visual file, a page-structure inventory, relationship objects, or a QA rubric.

All initial pages use byte-identical style-execution content and its same SHA-256. Different valid layouts are expected and do not change the shared style contract.

## 4. Apply relaxed page-local QA and repair

Make one combined QA evaluation containing exactly two qualitative decisions for the current page:

1. Does the page match the confirmed style overall?
2. Does it preserve the current Word page's main content, key facts, numbers, entities, qualifications, and main logic?

Return `pass`, `pass_with_advisory`, or `repair`, with repair scope `none`, `local`, or `structural`. Reasonable summarization, rewording, block reordering, visual translation, different valid layouts, small color variance, and small typography variance pass or receive an advisory.

Read the generated image once for both decisions. Do not run separate style, content, logic, OCR, or global-consistency QA passes on a normal page. Invoke deeper inspection only when that combined observation contains a concrete ambiguity or anomaly. A passing page proceeds directly to reconstruction.

Wrong facts, numbers, dates, entities, invented important conclusions, reversed major logic, unreadable crop/overlap, or material overall-style mismatch require repair.

- Local visible issues use `images/edits` with the current page image and concise issue-specific feedback.
- Structure, logic, or material overall-style issues use a fresh `images/generations` request with the same source/style contract plus concise feedback.

A repair on one page never blocks unrelated pages. `retry-page` releases only the matching active lease.
The confirmed automatic-repair budget limits local `images/edits`; once exhausted, the same page escalates to a fresh full-page `images/generations` repair and continues without another human confirmation.

## 5. Reconstruct accepted pages immediately

As soon as a page passes QA, dispatch its `reconstruct` action to `image-to-editable-ppt`. Record the resulting project-local editable page package:

```powershell
python scripts\word_to_editable_ppt.py workflow dispatch --project D:\Projects\Deck --page 1 --agent page-1 --attempt 2
editppt run record D:\Projects\Deck --page 1 --agent-id page-1 --attempt 2 --pptx D:\Projects\Deck\07_editable\page_001.pptx --artifact D:\Projects\Deck\07_editable\page_001.json
```

`image-to-editable-ppt` owns editable reconstruction and final assembly. The page cache identity includes the page-source hash, shared style hash, generation parameters, repair feedback, and reconstruction version. A changed source page invalidates only that page.

Generated page images are authoritative only under `06_images/generated/`. Final deck, mechanical QA, and run summary are authoritative only under `08_final/`.

## 6. Assemble and verify mechanically

Wait until every locked Word page is complete. Assemble completed page packages in locked Word order with one writer. Final checks are mechanical only:

- exact page count and order;
- required artifacts exist and hashes match;
- every page has a passing page-local QA result;
- editable objects exist;
- each page package is valid;
- the final PPTX opens; PowerPoint is the preferred back-renderer and LibreOffice is resolved only as an optional fallback;
- a strict project-local render proof is reused only when the final PPTX hash, renderer identity, expected page count, and retained proof-image hashes all match.

Do not re-evaluate visual similarity across pages. `officecli` may validate or surgically repair the already assembled file; it does not own page generation or assembly.

## Blockers

Stop for invalid pagination markers, unavailable physical pagination when the Word has no markers, an empty extracted page, a missing or changed frozen style artifact, an invalid page lease, a project-external artifact, a failed editable package, or page-count/order mismatch. Do not stop merely because LibreOffice is absent. When neither PowerPoint nor LibreOffice is available, complete structural PPTX validation and record a final render advisory. Never bypass the browser confirmation phase or mark a page complete before its reconstruction package is sealed.
